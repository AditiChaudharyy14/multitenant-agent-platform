"""The turn service: persistence after a turn, cancellation, and the per-turn model pin.

Runs against app_test as app_user, on the factory tenants (no real LLM, no KB search).
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.providers.base import (
    ChatRequest,
    Event,
    Finished,
    LLMProvider,
    RateLimited,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    Usage,
)
from app.agents.providers.budget import DailyBudget
from app.agents.providers.fake import FakeProvider, tool_call
from app.agents.providers.limiter import RpmLimiter
from app.agents.providers.router import ModelRouter, RetryPolicy
from app.agents.state import AgentEvent
from app.agents.tools.base import Tool, ToolResult
from app.agents.turn import TurnInput, start_turn
from app.db.session import tenant_tx
from app.domain.conversations import repo
from app.tenancy.context import ToolContext
from app.tenancy.orgs import get_org_by_id
from tests.factories import Tenant, set_owner_tenant

pytestmark = pytest.mark.usefixtures("as_app_user")


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str


async def echo(ctx: ToolContext, args: Args) -> ToolResult:
    return ToolResult(f"echo {args.q}", cards=[{"type": "echo"}])


TOOLS = {"echo": Tool("echo", "Echo.", Args, echo, "Echoing…")}


async def new_turn(t: Tenant, text_: str = "hello there") -> TurnInput:
    conv = t.conversations[t.x]
    base = ToolContext(org_id=t.org_id, user_id=t.x, role="customer", conversation_id=conv)
    async with tenant_tx(base.tenant) as conn:
        turn_id = await repo.add_user_message(conn, base, conv, text_)
        org = await get_org_by_id(conn, t.org_id)
    assert org is not None
    ctx = ToolContext(base.org_id, base.user_id, base.role, conv, turn_id)
    return TurnInput(org=org, ctx=ctx, history=[], user_text=text_)


async def drain(queue: asyncio.Queue[AgentEvent | None]) -> list[AgentEvent]:
    out = []
    while (ev := await queue.get()) is not None:
        out.append(ev)
    return out


async def assistant_row(owner: AsyncEngine, t: Tenant, turn_id: uuid.UUID):
    async with owner.begin() as conn:
        await set_owner_tenant(conn, t.org_id)
        msg = (
            await conn.execute(
                text(
                    "SELECT id, content, status, tool_calls, cards, citations FROM messages "
                    "WHERE conversation_id = :c AND role = 'assistant' AND created_at >= "
                    "(SELECT created_at FROM messages WHERE id = :t)"
                ),
                {"c": t.conversations[t.x], "t": turn_id},
            )
        ).one()
        run = (
            await conn.execute(
                text(
                    "SELECT provider, model, status, tools_called, input_tokens, output_tokens, latency_ms, error "
                    "FROM agent_runs WHERE turn_id = :t"
                ),
                {"t": turn_id},
            )
        ).one()
    return msg, run  # fmt: skip


async def test_completed_turn_is_persisted(tenants: tuple[Tenant, Tenant], owner_engine: AsyncEngine) -> None:
    t = tenants[0]
    inp = await new_turn(t)
    fake = FakeProvider([tool_call("echo", {"q": "x"}, id="e1"), "Done."])
    task, queue = start_turn(fake, inp, tools=TOOLS)
    events = await drain(queue)
    await task
    done = events[-1]
    assert done.type == "done"
    assert done.data["status"] == "complete" and done.data["content"] == "Done."
    assert done.data["turn_id"] == str(inp.ctx.turn_id)

    msg, run = await assistant_row(owner_engine, t, inp.ctx.turn_id)
    assert str(msg.id) == done.data["message_id"]
    assert (msg.content, msg.status) == ("Done.", "complete")
    assert msg.cards == [{"type": "echo"}]
    assert msg.tool_calls[0]["calls"][0]["name"] == "echo"
    assert msg.tool_calls[0]["calls"][0]["result"] == "echo x"
    assert (run.provider, run.model, run.status) == ("fake", "fake-model", "ok")
    assert run.tools_called == ["echo"]
    assert run.output_tokens > 0 and run.latency_ms is not None


class BlockingProvider(LLMProvider):
    """Streams one token, then hangs until cancelled, like a slow model mid-answer."""

    name = "blocking"

    async def stream(self, req: ChatRequest) -> AsyncIterator[Event]:
        yield TextDelta("Partial answer ")
        await asyncio.Event().wait()  # never set
        yield Finished("stop", Usage(), "blocking-model")


async def test_cancellation_persists_partial_message_as_stopped(
    tenants: tuple[Tenant, Tenant], owner_engine: AsyncEngine
) -> None:
    t = tenants[0]
    inp = await new_turn(t)
    task, queue = start_turn(BlockingProvider(), inp, tools=TOOLS)
    first = await queue.get()
    assert first is not None and first.type == "token"
    task.cancel()  # what a client disconnect or Stop does
    with contextlib.suppress(asyncio.CancelledError):
        await task

    msg, run = await assistant_row(owner_engine, t, inp.ctx.turn_id)
    assert (msg.content, msg.status) == ("Partial answer ", "stopped")
    assert run.status == "stopped"
    rest = await drain(queue)
    assert rest[-1].type == "done" and rest[-1].data["status"] == "stopped"


# ───────────── the model is pinned for the whole turn ─────────────


class StepAdapter:
    """Per model, a queue of outcomes: 'tool', 'text', or an exception to raise."""

    name = "scripted"

    def __init__(self, outcomes: dict[str, list]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def stream_once(self, model: str, req: ChatRequest) -> AsyncIterator[Event]:
        self.calls.append(model)
        outcome = self.outcomes[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "tool":
            yield ToolCallEvent(ToolCall(f"call-{len(self.calls)}", "echo", {"q": "x"}))
            yield Finished("tool_calls", Usage(1, 1), model)
        else:
            yield TextDelta(f"answer from {model}")
            yield Finished("stop", Usage(1, 1), model)


def make_router(adapter: StepAdapter, models: list[str]) -> ModelRouter:
    async def no_sleep(_: float) -> None:
        return None

    budget = DailyBudget(f"test-{uuid.uuid4().hex[:8]}", limit=100)
    limiters = {m: RpmLimiter(1000, sleep=no_sleep) for m in models}
    return ModelRouter(
        adapter, models, budget, limiters, RetryPolicy(max_retries=3), sleep=no_sleep, jitter=lambda: 0.5
    )


def busy() -> RateLimited:
    return RateLimited("busy", retry_after=None, daily=False)


async def test_mid_turn_failure_ends_turn_without_switching_models(
    tenants: tuple[Tenant, Tenant], owner_engine: AsyncEngine
) -> None:
    t = tenants[0]
    inp = await new_turn(t)
    # Step 1 is answered by a. On step 2, a stays rate limited past every retry. Unpinned,
    # the router would fall back to b; pinned, the turn must end with an error instead.
    adapter = StepAdapter({"a": ["tool", busy(), busy(), busy(), busy()], "b": ["text"]})
    task, queue = start_turn(make_router(adapter, ["a", "b"]), inp, tools=TOOLS)
    events = await drain(queue)
    await task

    assert "b" not in adapter.calls
    assert adapter.calls == ["a"] * 5  # step 1, then step 2 plus its 3 retries
    types = [e.type for e in events]
    assert types[-2:] == ["error", "done"]
    assert events[-2].data["code"] == "rate_limited"
    assert events[-1].data["status"] == "error"
    assert "tool_end" in types  # what was produced before the failure was streamed

    msg, run = await assistant_row(owner_engine, t, inp.ctx.turn_id)
    assert msg.status == "error"
    assert msg.tool_calls[0]["calls"][0]["name"] == "echo"  # kept
    assert (run.model, run.status) == ("a", "rate_limited")
    assert run.error


async def test_fallback_before_the_first_step_pins_the_fallback_model(
    tenants: tuple[Tenant, Tenant], owner_engine: AsyncEngine
) -> None:
    t = tenants[0]
    inp = await new_turn(t)
    # a's daily quota is gone before anything streams: b is chosen and keeps the turn.
    adapter = StepAdapter({"a": [RateLimited("quota", None, daily=True)], "b": ["tool", "text"]})
    task, queue = start_turn(make_router(adapter, ["a", "b"]), inp, tools=TOOLS)
    events = await drain(queue)
    await task
    assert adapter.calls == ["a", "b", "b"]
    assert events[-1].data["status"] == "complete"
    _, run = await assistant_row(owner_engine, t, inp.ctx.turn_id)
    assert (run.model, run.status) == ("b", "ok")


def test_pin_returns_a_single_model_router() -> None:
    adapter = StepAdapter({})
    r = make_router(adapter, ["a", "b"])
    pinned = r.pin("b")
    assert isinstance(pinned, ModelRouter) and pinned.models == ["b"]
    assert pinned.budget is r.budget and pinned.limiters["b"] is r.limiters["b"]
    assert r.models == ["a", "b"]  # the shared router is untouched
    with pytest.raises(ValueError):
        r.pin("zzz")
    fake = FakeProvider()
    assert fake.pin("fake-model") is fake
