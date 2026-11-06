"""The turn service: runs the agent loop as its own task and persists what it produced.

The loop runs in a separate asyncio task that feeds a queue, and the SSE response only
reads that queue. Stop and client disconnects cancel the task. Either way, the task saves
the assistant message and the agent_runs row before it ends, under asyncio.shield so a
second cancel can't cut that write short.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app import clock
from app.agents.loop import run_turn
from app.agents.prompts.system import build_system_prompt
from app.agents.providers.base import LLMProvider, Message
from app.agents.state import AgentEvent, TurnState
from app.agents.tools.base import Tool, ToolResult, error_result
from app.agents.tools.registry import tools_for
from app.db.session import tenant_tx
from app.domain.conversations import repo
from app.tenancy.context import ToolContext
from app.tenancy.orgs import Org

log = logging.getLogger("app.agent")

Queue = asyncio.Queue[AgentEvent | None]  # None: the turn is over, nothing more will come

# Strong references: the event loop keeps only weak ones to tasks, and a turn must finish
# (and persist) even after its HTTP response is gone.
_running: set[asyncio.Task[None]] = set()


@dataclass(frozen=True, slots=True)
class ServerAction:
    """A card button (Confirm on a proposal, a time slot). The server runs `tool`, one of the
    org's own vertical's tools, with `arguments` before the model runs: the model decides
    nothing about it. The tool validates the arguments like any model call would."""

    tool: str  # e.g. confirm_proposal, create_booking, reschedule_booking
    arguments: dict[str, Any]
    pressed: str  # what the customer pressed, in words, for the model


@dataclass(frozen=True, slots=True)
class TurnInput:
    org: Org
    ctx: ToolContext  # conversation_id and turn_id set
    history: list[Message]
    user_text: str
    action: ServerAction | None = None


ACTION_NOTE = """
SERVER ACTION THIS TURN (trusted, done by the server, not by you): the customer pressed \
{pressed}, so the server ran {tool}. The result was:
{result}
Tell the customer this result in one or two sentences (an order or booking number if there \
is one). Do not call {tool} for it. If it says PROPOSED, show the summary briefly and ask \
them to confirm. If it says CHANGED, briefly explain what changed and ask them to confirm \
the new summary. If it says REFUSED, say why and offer the alternatives it lists. Speak \
to the customer as yourself; don't mention "the server".
"""


def start_turn(provider: LLMProvider, inp: TurnInput, *, tools: dict[str, Tool] | None = None) -> tuple[asyncio.Task[None], Queue]:
    queue: Queue = asyncio.Queue()
    chosen = tools if tools is not None else tools_for(inp.org.vertical)  # keyed on vertical only
    task = asyncio.create_task(_run(provider, inp, chosen, queue))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task, queue


async def _run(provider: LLMProvider, inp: TurnInput, tools: dict[str, Tool], queue: Queue) -> None:
    state = TurnState()
    started = time.monotonic()
    cancelled = False
    try:
        system = build_system_prompt(inp.org, clock.now())
        if inp.action is not None:
            # Deterministic: the action runs here, before (and regardless of) any model call.
            result = await _server_action(inp, tools, state, queue)
            system += ACTION_NOTE.format(pressed=inp.action.pressed, tool=inp.action.tool, result=result.content_for_model)
            # The server already ran it; the model only acknowledges, so it isn't offered
            # that tool at all (a prompt alone didn't stop it from confirming again).
            tools = {name: t for name, t in tools.items() if name != inp.action.tool}
        async for event in run_turn(provider, system=system, history=inp.history, user_text=inp.user_text,
                                    tools=tools, ctx=inp.ctx, state=state):  # fmt: skip
            queue.put_nowait(event)
    except asyncio.CancelledError:
        cancelled = True
        state.status = "stopped"
    except Exception as exc:  # a bug, not a provider failure: still end the turn cleanly
        log.exception("turn %s crashed", inp.ctx.turn_id)
        state.status, state.error = "error", f"{type(exc).__name__}: {exc}"
        queue.put_nowait(AgentEvent("error", {"code": "internal_error",
                                              "message": "Sorry, something went wrong. Please try again."}))  # fmt: skip
    state.finalize()
    latency_ms = int((time.monotonic() - started) * 1000)
    message_id = await asyncio.shield(_persist(inp.ctx, state, latency_ms))
    queue.put_nowait(AgentEvent("done", {
        "message_id": str(message_id) if message_id else None, "turn_id": str(inp.ctx.turn_id),
        "status": repo.MESSAGE_STATUS[state.status], "content": state.content,
        "citations": [{"id": c.id, "title": c.title} for c in state.citations],
    }))  # fmt: skip
    queue.put_nowait(None)
    if cancelled:
        raise asyncio.CancelledError  # the task really was cancelled; say so to whoever awaits it


async def _server_action(inp: TurnInput, tools: dict[str, Tool], state: TurnState, queue: Queue) -> ToolResult:
    """Run the button's tool, stream it like a tool call, and record it in the turn."""
    assert inp.action is not None
    call_id, name = f"server_{inp.action.tool}_{inp.ctx.turn_id}", inp.action.tool
    tool = tools.get(name)
    queue.put_nowait(AgentEvent("tool_start", {"call_id": call_id, "name": name,
                                               "label": tool.activity_label if tool else name}))  # fmt: skip
    if tool is None:  # the API checks the vertical first; this only guards direct callers
        result = error_result(f"{name} is not available here")
    else:
        try:
            args = tool.args_model.model_validate(inp.action.arguments)
        except ValidationError as exc:
            result = error_result(f"invalid button: {exc.errors(include_url=False)}")
        else:
            result = await tool.handler(inp.ctx, args)  # ctx comes from the session, never the request
    state.tools_called.append(f"server:{name}")
    state.cards.extend(result.cards)
    for card in result.cards:
        queue.put_nowait(AgentEvent("card", {"card": card}))
    queue.put_nowait(AgentEvent("tool_end", {"call_id": call_id, "name": name, "ok": result.ok,
                                             "label": result.activity_label or ("Done" if result.ok else "Failed")}))  # fmt: skip
    # Recorded like a tool step, so later turns' history shows what the server did.
    state.steps.append({"step": -1, "text": "", "server": True, "calls": [{
        "id": call_id, "name": name, "arguments": inp.action.arguments,
        "provider_meta": None, "result": result.content_for_model, "ok": result.ok,
    }]})  # fmt: skip
    return result


async def _persist(ctx: ToolContext, state: TurnState, latency_ms: int):  # noqa: ANN202
    """Assistant message + agent_runs row in one transaction. Never raises."""
    try:
        async with tenant_tx(ctx.tenant) as conn:
            message_id = await repo.save_assistant(conn, ctx, state)
            await repo.save_run(conn, ctx, state, latency_ms)
        return message_id
    except Exception:
        log.exception("could not persist turn %s", ctx.turn_id)
        return None
