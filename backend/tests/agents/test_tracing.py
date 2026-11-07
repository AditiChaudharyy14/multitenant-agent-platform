"""OpenTelemetry spans for agent turns, checked with an in-memory exporter.

One `agent.run` span per turn, with a child `llm.call` per model call (model, tokens) and a
child `tool.call` per tool call (name, duration, status). Attributes carry the org slug and
conversation id, never a user's email or any message text.
"""

import uuid
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.observability import tracing
from tests.agents.turn_support import run_customer_turn
from tests.factories import Shop, set_owner_tenant

pytestmark = pytest.mark.usefixtures("as_app_user")

_EXPORTER = InMemorySpanExporter()
ALLOWED_KEYS = {
    "org.slug", "org.vertical", "conversation.id", "turn.id", "agent", "status", "steps", "tool_calls",
    "gen_ai.system", "gen_ai.request.model", "gen_ai.response.model", "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens", "gen_ai.response.finish_reason", "llm.step",
    "tool.name", "tool.duration_ms", "tool.status", "error.type",
}  # fmt: skip


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """A real SDK provider with an in-memory exporter (installed once per process: the
    global tracer provider can be set only once)."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(provider)
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


async def _conversation(owner: AsyncEngine, shop: Shop, user: uuid.UUID) -> uuid.UUID:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        return await conn.scalar(text("INSERT INTO conversations (org_id, user_id) VALUES (:o, :u) RETURNING id"),
                                 {"o": shop.org_id, "u": user})  # fmt: skip


def by_name(exporter: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


async def test_a_turn_is_one_run_span_with_llm_and_tool_children(
    spans: InMemorySpanExporter, shop: Shop, owner_engine: AsyncEngine
) -> None:  # fmt: skip
    conv = await _conversation(owner_engine, shop, shop.x)
    await run_customer_turn(shop, shop.x, conv, "what green teas do you have?", script=[
        {"tool": "search_products", "args": {"category": "green"}}, {"text": "Here they are."}])  # fmt: skip
    (run,) = by_name(spans, "agent.run")
    llm = by_name(spans, "llm.call")
    tools = by_name(spans, "tool.call")
    assert len(llm) == 2 and len(tools) == 1
    for child in (*llm, *tools):  # same trace, parented on the run
        assert child.context.trace_id == run.context.trace_id
        assert child.parent is not None and child.parent.span_id == run.context.span_id
    assert run.parent is None
    slug = (await _slug(owner_engine, shop))
    assert run.attributes["org.slug"] == slug and run.attributes["org.vertical"] == "store"
    assert run.attributes["conversation.id"] == str(conv) and run.attributes["status"] == "ok"
    assert run.attributes["tool_calls"] == 1
    assert llm[0].attributes["gen_ai.request.model"] == "fake-model"
    assert llm[0].attributes["gen_ai.usage.output_tokens"] >= 1 and "gen_ai.usage.input_tokens" in llm[0].attributes
    t = tools[0].attributes
    assert t["tool.name"] == "search_products" and t["tool.status"] == "ok" and t["tool.duration_ms"] >= 0


async def test_a_failed_tool_is_marked_error(spans: InMemorySpanExporter, shop: Shop, owner_engine: AsyncEngine) -> None:
    conv = await _conversation(owner_engine, shop, shop.x)
    await run_customer_turn(shop, shop.x, conv, "remember this", script=[
        {"tool": "remember_preference", "args": {"key": "usual_products", "value": "x"}}, {"text": "Sorry."}])  # fmt: skip
    (tool,) = by_name(spans, "tool.call")
    assert tool.attributes["tool.status"] == "error"
    assert tool.status.status_code == trace.StatusCode.ERROR


async def test_no_user_email_or_message_text_in_any_span(spans: InMemorySpanExporter, shop: Shop, owner_engine: AsyncEngine) -> None:
    secret_text = "my secret plan is to buy jasmine pearls for Aunt Kamala"
    conv = await _conversation(owner_engine, shop, shop.x)
    async with owner_engine.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        email = await conn.scalar(text("SELECT email FROM users WHERE id = :u"), {"u": shop.x})
    await run_customer_turn(shop, shop.x, conv, secret_text, script=[
        {"tool": "search_products", "args": {"query": "jasmine"}}, {"text": "Reply text that must not be traced."}])  # fmt: skip
    finished = spans.get_finished_spans()
    assert finished
    for s in finished:
        assert set(s.attributes) <= ALLOWED_KEYS, (s.name, set(s.attributes) - ALLOWED_KEYS)
        values = " ".join(str(v) for v in s.attributes.values())
        for forbidden in (email, "secret plan", "Aunt Kamala", "jasmine", "must not be traced", str(shop.x)):
            assert forbidden not in values, (s.name, forbidden)
        for event in s.events:  # exceptions recorded on spans carry no message text either
            assert "secret plan" not in str(event.attributes)


def test_without_an_endpoint_tracing_is_off() -> None:
    assert tracing.build_provider(endpoint="", service_name="x") is None


def test_with_an_endpoint_spans_are_exported_over_otlp_http() -> None:
    provider = tracing.build_provider(endpoint="http://jaeger:4318", service_name="multitenant-backend")
    assert provider is not None
    assert provider.resource.attributes["service.name"] == "multitenant-backend"
    provider.shutdown()


async def _slug(owner: AsyncEngine, shop: Shop) -> str:
    async with owner.begin() as conn:
        return await conn.scalar(text("SELECT slug FROM orgs WHERE id = :o"), {"o": shop.org_id})
