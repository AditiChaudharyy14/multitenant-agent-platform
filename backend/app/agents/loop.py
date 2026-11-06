"""The agent loop: one user turn, from the user's message to the model's final answer.

No framework. Each step sends the conversation to the model and streams its reply; if the
model asks for tools, they run with the server-built ToolContext, their results join the
conversation, and the next step goes back to the model. The turn ends when the model
answers with text only, or after MAX_STEPS.

The loop never touches the database itself (tools do, through their own tenant
transactions) and never persists anything: it fills a TurnState that the turn service
(app/agents/turn.py) saves, even when the turn is cancelled halfway.
"""

import asyncio
import logging
from collections.abc import AsyncIterator

from pydantic import ValidationError

from app.agents.providers.base import (
    ChatRequest,
    DailyBudgetExhausted,
    Finished,
    LLMError,
    LLMProvider,
    Message,
    ProviderUnavailable,
    RateLimited,
    TextDelta,
    ToolCall,
    ToolCallEvent,
)
from app.agents.state import AgentEvent, TurnState
from app.agents.tools.base import Tool, ToolResult, error_result
from app.tenancy.context import ToolContext

log = logging.getLogger("app.agent")

MAX_STEPS = 6  # model requests per turn; a model that keeps calling tools is cut off here

# Quota-type failures: the user should simply try again later.
_RATE_LIMITED = (DailyBudgetExhausted, ProviderUnavailable, RateLimited)

# What the user sees for each error code. Internal details go to agent_runs.error only.
USER_MESSAGES = {
    "rate_limited": "I'm getting a lot of requests right now. Please try again in a minute.",
    "provider_error": "Sorry, something went wrong on my side. Please try again.",
    "max_steps": "Sorry, I couldn't finish that request. Could you try asking in a simpler way?",
}


async def run_turn(
    provider: LLMProvider,
    *,
    system: str,
    history: list[Message],
    user_text: str,
    tools: dict[str, Tool],
    ctx: ToolContext,
    state: TurnState,
) -> AsyncIterator[AgentEvent]:
    """Run one turn, yielding token/tool_start/card/tool_end/citation/error events.

    `done` is not yielded here: the turn service sends it after persisting, so it can
    carry the saved message id.
    """
    messages = [*history, Message("user", user_text)]  # this turn's growing conversation
    specs = [t.spec() for t in tools.values()]  # JSON schemas the model sees (no identity)
    state.provider = provider.name
    try:
        for step in range(MAX_STEPS):
            text: list[str] = []  # this step's text (the model may talk before calling tools)
            calls: list[ToolCall] = []
            async for ev in provider.stream(ChatRequest(system, messages, specs)):
                if isinstance(ev, TextDelta):
                    text.append(ev.text)
                    state.text_parts.append(ev.text)  # kept at once, so a cancel keeps it
                    yield AgentEvent("token", {"text": ev.text})
                elif isinstance(ev, ToolCallEvent):
                    calls.append(ev.call)  # run after the stream ends, in order
                elif isinstance(ev, Finished):
                    state.add_usage(ev.usage)
                    if step == 0:
                        # Pin the turn to whichever model answered first (the router may have
                        # fallen back before it). Later steps never switch models: if the
                        # pinned one fails after its retries, the turn ends with an error.
                        state.model = ev.model
                        provider = provider.pin(ev.model)

            if not calls:  # text only: the final answer
                state.finalize()  # drop citations that weren't retrieved this turn
                state.status = "ok"
                for c in state.citations:
                    yield AgentEvent("citation", {"id": c.id, "title": c.title})
                return

            if step == MAX_STEPS - 1:
                break  # no step left to read the results, so don't run (or mutate) anything

            # The assistant's tool request goes into the conversation before its results,
            # with provider_meta (e.g. Gemini's thoughtSignature) intact inside each call.
            messages.append(Message("assistant", "".join(text), tool_calls=calls))
            record: dict = {"step": step, "text": "".join(text), "calls": []}
            state.steps.append(record)  # appended before running, so a cancel keeps it

            for call in calls:
                tool = tools.get(call.name)  # None: the model made up a tool name
                label = tool.activity_label if tool else "Working…"
                yield AgentEvent("tool_start", {"call_id": call.id, "name": call.name, "label": label})
                result = await _execute(tool, call, ctx)
                state.tools_called.append(call.name)
                # Every source returned this turn becomes citable in the final answer.
                state.sources.update({c.id: c for c in result.citations})
                state.cards.extend(result.cards)
                for card in result.cards:
                    yield AgentEvent("card", {"card": card})
                yield AgentEvent("tool_end", {
                    "call_id": call.id, "name": call.name,
                    "label": result.activity_label or label, "ok": result.ok,
                })  # fmt: skip
                record["calls"].append({
                    "id": call.id, "name": call.name, "arguments": call.arguments,
                    "provider_meta": call.provider_meta,
                    "result": result.content_for_model, "ok": result.ok,
                })  # fmt: skip
                # The result answers the call by id; the model reads it on the next step.
                messages.append(Message("tool", result.content_for_model, tool_call_id=call.id, name=call.name))

        # Only reached via the break above: the model was still calling tools at the cap.
        state.finalize()
        _fail(state, "error", f"max_steps: still calling tools after {MAX_STEPS} steps")
        yield AgentEvent("error", {"code": "max_steps", "message": USER_MESSAGES["max_steps"]})
    except asyncio.CancelledError:
        # Stop or client disconnect. Re-raised so the task really ends; the turn service
        # sees status 'stopped' and persists the partial answer.
        state.status = "stopped"
        raise
    except LLMError as exc:
        # The provider failed after its own retries. No other model is tried mid-turn.
        state.finalize()
        code = "rate_limited" if isinstance(exc, _RATE_LIMITED) else "provider_error"
        _fail(state, "rate_limited" if code == "rate_limited" else "error", f"{type(exc).__name__}: {exc}")
        yield AgentEvent("error", {"code": code, "message": USER_MESSAGES[code]})


async def _execute(tool: Tool | None, call: ToolCall, ctx: ToolContext) -> ToolResult:
    """Run one tool call. Every failure becomes an error result for the model, never a crash."""
    if tool is None:
        return error_result(f"unknown tool {call.name!r}. Use only the tools you were given.")
    try:
        # Validation is the gate: a model can't pass a field the schema doesn't declare
        # (extra="forbid"), so it can never supply org_id or user_id.
        args = tool.args_model.model_validate(call.arguments)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
        return error_result(f"invalid arguments for {tool.name}: {problems}. Fix them and call it again.")
    try:
        return await tool.handler(ctx, args)  # ctx comes from the session, never from the model
    except Exception:
        # Log the details; tell the model only that it failed, so internals never leak.
        log.exception("tool %s failed (org=%s turn=%s)", tool.name, ctx.org_id, ctx.turn_id)
        return error_result(f"{tool.name} failed because of an internal error. Tell the user something went wrong.")


def _fail(state: TurnState, status: str, reason: str) -> None:
    state.status = status
    state.error = reason
    log.warning("turn failed (%s): %s", status, reason)
