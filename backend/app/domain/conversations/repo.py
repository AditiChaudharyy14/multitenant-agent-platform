"""Conversations and messages, always inside the caller's tenant_tx.

RLS already limits a customer to their own rows. Every query here also filters on
user_id = ctx.user_id explicitly: an admin (whom RLS lets see the whole org) still gets
only their own chats through this API, and a policy mistake would not be enough to leak.
A miss (foreign, deleted or nonexistent) returns None, and the API turns it into one 404.

`kind` separates the customer chat from the admin analytics chat: every lookup matches
it, so an admin conversation id sent to a customer route (or the reverse) is a 404. The
admin_kind RLS policy also hides admin conversations from any non-admin context.
"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.agents.ids import new_id
from app.agents.state import TurnState
from app.tenancy.context import ToolContext

TITLE_WORDS = 6
TITLE_MAX = 120
_CONV_COLUMNS = "id, title, created_at, updated_at"
_OWN = "user_id = :u AND kind = :k AND deleted_at IS NULL"
KINDS = ("customer", "admin")

# messages.status for each agent_runs.status.
MESSAGE_STATUS = {"ok": "complete", "stopped": "stopped", "error": "error", "rate_limited": "error"}


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown conversation kind {kind!r}")
    return kind


def auto_title(content: str) -> str:
    """The first six words of the first message: no LLM call."""
    return " ".join(content.split()[:TITLE_WORDS])[:TITLE_MAX] or "New chat"


async def create(conn: AsyncConnection, ctx: ToolContext, kind: str = "customer") -> dict[str, Any]:
    row = (
        await conn.execute(
            text(f"INSERT INTO conversations (org_id, user_id, kind) VALUES (:o, :u, :k) RETURNING {_CONV_COLUMNS}"),
            {"o": ctx.org_id, "u": ctx.user_id, "k": _kind(kind)},
        )
    ).one()
    return dict(row._mapping)


async def list_own(conn: AsyncConnection, ctx: ToolContext, kind: str = "customer") -> list[dict[str, Any]]:
    rows = await conn.execute(
        text(f"SELECT {_CONV_COLUMNS} FROM conversations WHERE {_OWN} ORDER BY updated_at DESC, id"),
        {"u": ctx.user_id, "k": _kind(kind)},
    )
    return [dict(r._mapping) for r in rows]


async def rename(conn: AsyncConnection, ctx: ToolContext, cid: UUID, title: str, kind: str = "customer") -> dict[str, Any] | None:
    row = (
        await conn.execute(
            text(
                f"UPDATE conversations SET title = :t, updated_at = now() WHERE id = :c AND {_OWN} "
                f"RETURNING {_CONV_COLUMNS}"
            ),
            {"t": title, "c": cid, "u": ctx.user_id, "k": _kind(kind)},
        )
    ).one_or_none()
    return dict(row._mapping) if row else None


async def soft_delete(conn: AsyncConnection, ctx: ToolContext, cid: UUID, kind: str = "customer") -> bool:
    res = await conn.execute(
        text(f"UPDATE conversations SET deleted_at = now() WHERE id = :c AND {_OWN}"),
        {"c": cid, "u": ctx.user_id, "k": _kind(kind)},
    )
    return res.rowcount == 1


async def exists(conn: AsyncConnection, ctx: ToolContext, cid: UUID, kind: str = "customer") -> bool:
    found = await conn.scalar(
        text(f"SELECT 1 FROM conversations WHERE id = :c AND {_OWN}"), {"c": cid, "u": ctx.user_id, "k": _kind(kind)}
    )
    return found is not None


def _public_tool_calls(steps: list[dict] | None) -> list[dict] | None:
    """What the UI may see of a turn's tool calls: no raw results, no vendor metadata."""
    if steps is None:
        return None
    return [
        {"step": s["step"], "calls": [{"id": c["id"], "name": c["name"], "arguments": c["arguments"],
                                       "ok": c["ok"]} for c in s["calls"]]}  # fmt: skip
        for s in steps
    ]


async def list_messages(conn: AsyncConnection, ctx: ToolContext, cid: UUID, kind: str = "customer") -> list[dict[str, Any]] | None:
    if not await exists(conn, ctx, cid, kind):
        return None
    rows = await conn.execute(
        text(
            "SELECT id, role, content, status, tool_calls, citations, cards, created_at FROM messages "
            "WHERE conversation_id = :c AND user_id = :u ORDER BY created_at, id"
        ),
        {"c": cid, "u": ctx.user_id},
    )
    out = []
    for r in rows:
        m = dict(r._mapping)
        m["tool_calls"] = _public_tool_calls(m["tool_calls"])
        out.append(m)
    return out


async def recent_messages(conn: AsyncConnection, ctx: ToolContext, cid: UUID, limit: int) -> list[Any]:
    """The conversation's last `limit` messages, oldest first, for the model's history."""
    rows = await conn.execute(
        text(
            "SELECT role, content, tool_calls FROM ("
            "  SELECT role, content, tool_calls, created_at, id FROM messages"
            "  WHERE conversation_id = :c AND user_id = :u ORDER BY created_at DESC, id DESC LIMIT :n"
            ") recent ORDER BY created_at, id"
        ),
        {"c": cid, "u": ctx.user_id, "n": limit},
    )
    return list(rows)


async def add_user_message(
    conn: AsyncConnection, ctx: ToolContext, cid: UUID, content: str, kind: str = "customer"
) -> UUID | None:
    """Store the user's message (its id is the turn id) and auto-title a new conversation."""
    # FOR UPDATE: two first messages sent at once can't both decide they're first.
    found = await conn.scalar(
        text(f"SELECT 1 FROM conversations WHERE id = :c AND {_OWN} FOR UPDATE"),
        {"c": cid, "u": ctx.user_id, "k": _kind(kind)},
    )
    if found is None:
        return None
    first = not await conn.scalar(
        text("SELECT EXISTS (SELECT 1 FROM messages WHERE conversation_id = :c AND role = 'user')"), {"c": cid}
    )
    turn_id = await conn.scalar(
        text(
            "INSERT INTO messages (id, org_id, user_id, conversation_id, role, content) "
            "VALUES (:id, :o, :u, :c, 'user', :t) RETURNING id"
        ),
        # The turn id ends up in server-action call ids the model reads back, so it comes
        # from new_id() (deterministic under evals), not gen_random_uuid().
        {"id": new_id(), "o": ctx.org_id, "u": ctx.user_id, "c": cid, "t": content},
    )
    await conn.execute(
        text(
            "UPDATE conversations SET updated_at = now(), title = CASE WHEN :first THEN :title ELSE title END "
            "WHERE id = :c"
        ),
        {"first": first, "title": auto_title(content), "c": cid},
    )
    return turn_id


async def save_assistant(conn: AsyncConnection, ctx: ToolContext, state: TurnState) -> UUID:
    return await conn.scalar(
        text(
            "INSERT INTO messages (org_id, user_id, conversation_id, role, content, status, tool_calls, "
            "citations, cards) VALUES (:o, :u, :c, 'assistant', :t, :s, CAST(:tc AS jsonb), "
            "CAST(:ci AS jsonb), CAST(:ca AS jsonb)) RETURNING id"
        ),
        {
            "o": ctx.org_id, "u": ctx.user_id, "c": ctx.conversation_id, "t": state.content,
            "s": MESSAGE_STATUS[state.status],
            "tc": json.dumps(state.steps), "ci": json.dumps([{"id": c.id, "title": c.title} for c in state.citations]),
            "ca": json.dumps(state.cards),
        },
    )  # fmt: skip


async def save_run(conn: AsyncConnection, ctx: ToolContext, state: TurnState, latency_ms: int) -> None:
    await conn.execute(
        text(
            "INSERT INTO agent_runs (org_id, user_id, conversation_id, turn_id, provider, model, input_tokens, "
            "output_tokens, latency_ms, tools_called, status, error) VALUES (:o, :u, :c, :turn, :p, :m, :it, "
            ":ot, :lat, CAST(:tools AS jsonb), :s, :e)"
        ),
        {
            "o": ctx.org_id, "u": ctx.user_id, "c": ctx.conversation_id, "turn": ctx.turn_id,
            "p": state.provider or "unknown", "m": state.model or "unknown",
            "it": state.input_tokens, "ot": state.output_tokens, "lat": latency_ms,
            "tools": json.dumps(state.tools_called), "s": state.status, "e": state.error,
        },
    )  # fmt: skip
