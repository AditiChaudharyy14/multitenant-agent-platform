"""OpenTelemetry tracing for agent turns: one span per run, children per LLM and tool call.

    agent.run      org.slug, org.vertical, conversation.id, turn.id, agent, status, steps, tool_calls
      llm.call     gen_ai.request.model / response.model, input/output tokens, finish reason
      tool.call    tool.name, tool.duration_ms, tool.status (ok | error)

Privacy: attributes are ids, names and numbers only. No user email, no user id, no
message text, no tool arguments or results; an exception is recorded by type only.

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (`make up-traces` sets it and starts Jaeger):
without a provider the OpenTelemetry API is a no-op, so this costs nothing by default.
The tracer is looked up per call, so a provider installed later (or by a test) is used.
"""

import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from app.agents.providers.base import ChatRequest, Event, Finished, LLMProvider
from app.agents.tools.base import Tool, ToolResult
from app.tenancy.context import ToolContext

log = logging.getLogger("app.tracing")


def tracer() -> trace.Tracer:
    return trace.get_tracer("app.agent")


def build_provider(*, endpoint: str, service_name: str) -> TracerProvider | None:
    """An SDK provider exporting over OTLP/HTTP to `endpoint`, or None when it is empty."""
    if not endpoint:
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")))
    return provider


def setup_from_env() -> TracerProvider | None:
    """Install the global provider from the standard OTEL_* variables (app startup)."""
    provider = build_provider(endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
                              service_name=os.environ.get("OTEL_SERVICE_NAME", "multitenant-backend"))  # fmt: skip
    if provider is not None:
        trace.set_tracer_provider(provider)
        log.info("tracing on: exporting spans over OTLP/HTTP")
    return provider


def run_attributes(*, slug: str, vertical: str, conversation_id: Any, turn_id: Any, agent: str) -> dict[str, str]:
    return {"org.slug": slug, "org.vertical": vertical, "conversation.id": str(conversation_id),
            "turn.id": str(turn_id), "agent": agent}  # fmt: skip


class TracingProvider(LLMProvider):
    """Wraps a provider: every stream() is one llm.call span under the current span.

    The span is started and ended explicitly instead of being made "current": stream() is
    an async generator, and attaching a context there would leak into the caller between
    yields (and fail to detach if the generator is closed elsewhere)."""

    def __init__(self, inner: LLMProvider, step: list[int] | None = None) -> None:
        self.inner = inner
        self.name = inner.name
        self._step = step if step is not None else [0]  # shared across pin(): numbers the calls

    def pin(self, model: str) -> LLMProvider:
        return TracingProvider(self.inner.pin(model), self._step)

    async def stream(self, req: ChatRequest) -> AsyncIterator[Event]:
        span = tracer().start_span("llm.call", attributes={"gen_ai.system": self.inner.name, "llm.step": self._step[0]})
        self._step[0] += 1
        try:
            async for ev in self.inner.stream(req):
                if isinstance(ev, Finished):
                    span.set_attributes({
                        "gen_ai.request.model": ev.model, "gen_ai.response.model": ev.model,
                        "gen_ai.usage.input_tokens": ev.usage.input_tokens,
                        "gen_ai.usage.output_tokens": ev.usage.output_tokens,
                        "gen_ai.response.finish_reason": ev.finish_reason,
                    })  # fmt: skip
                yield ev
        except BaseException as exc:
            _error(span, exc)
            raise
        finally:
            span.end()


def _error(span: Span, exc: BaseException) -> None:
    span.set_attribute("error.type", type(exc).__name__)  # type only: messages may hold user text
    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))


def traced_tool(tool: Tool) -> Tool:
    """The same tool, its handler wrapped in a tool.call span (name, duration, status)."""

    async def handler(ctx: ToolContext, args: Any) -> ToolResult:
        started = time.monotonic()
        with tracer().start_as_current_span("tool.call", attributes={"tool.name": tool.name},
                                            record_exception=False, set_status_on_exception=False) as span:  # fmt: skip
            try:
                result = await tool.handler(ctx, args)
            except BaseException as exc:
                span.set_attributes({"tool.status": "error", "tool.duration_ms": _ms(started)})
                _error(span, exc)
                raise
            span.set_attributes({"tool.status": "ok" if result.ok else "error", "tool.duration_ms": _ms(started)})
            if not result.ok:
                span.set_status(Status(StatusCode.ERROR, "tool returned an error"))
            return result

    return replace(tool, handler=handler)


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 2)
