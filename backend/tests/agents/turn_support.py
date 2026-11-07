"""Run one real turn (turn.start_turn, the real tools and prompt) on the FakeProvider.

`script` steps: {"tool": name, "args": {...}} or {"text": "..."}, one model call each.
"""

import asyncio
import uuid
from typing import Any

from sqlalchemy import text

from app.agents.providers.base import ChatRequest
from app.agents.providers.fake import FakeProvider, tool_call
from app.agents.state import AgentEvent
from app.agents.turn import TurnInput, start_turn
from app.db.session import tenant_tx
from app.domain.conversations import repo
from app.tenancy.context import ToolContext
from app.tenancy.orgs import get_org_by_id
from tests.factories import Shop, Studio


async def run_customer_turn(org: Shop | Studio, user: uuid.UUID, conversation: uuid.UUID, user_text: str, *,
                            script: list[dict[str, Any]], seen: list[ChatRequest] | None = None,
                            agent: str = "customer") -> list[AgentEvent]:  # fmt: skip
    role = "admin" if agent == "admin" else "customer"
    base = ToolContext(org_id=org.org_id, user_id=user, role=role, conversation_id=conversation)
    async with tenant_tx(base.tenant) as conn:
        turn_id = await repo.add_user_message(conn, base, conversation, user_text)
        org_row = await get_org_by_id(conn, org.org_id)
    assert org_row is not None
    ctx = ToolContext(base.org_id, base.user_id, base.role, conversation, turn_id)
    fake = FakeProvider([tool_call(s["tool"], s.get("args", {})) if "tool" in s else s["text"] for s in script])
    task, queue = start_turn(fake, TurnInput(org=org_row, ctx=ctx, history=[], user_text=user_text, agent=agent))
    events = []
    while (ev := await queue.get()) is not None:
        events.append(ev)
    await asyncio.wait_for(task, 10)
    if seen is not None:
        seen.extend(fake.requests)
    return events


async def system_prompt_for(org: Shop | Studio, user: uuid.UUID, *, agent: str = "customer") -> str:
    """The system prompt one turn of `agent` sends for `user`, in a fresh conversation."""
    role = "admin" if agent == "admin" else "customer"
    async with tenant_tx(ToolContext(org.org_id, user, role).tenant) as conn:
        conv = await conn.scalar(text("INSERT INTO conversations (org_id, user_id, kind) VALUES (:o, :u, :k) RETURNING id"),
                                 {"o": org.org_id, "u": user, "k": agent})  # fmt: skip
    seen: list[ChatRequest] = []
    await run_customer_turn(org, user, conv, "hello", script=[{"text": "Hi."}], seen=seen, agent=agent)
    return seen[0].system
