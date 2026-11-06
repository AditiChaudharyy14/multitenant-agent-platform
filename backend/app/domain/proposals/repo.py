"""Proposal rows. A proposal is exactly what will be committed (payload) plus what the
customer was shown (summary). Vertical-agnostic: store and booking both use this.

Every query filters on user_id and conversation_id explicitly, on top of RLS.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app import clock
from app.tenancy.context import ToolContext

PROPOSAL_TTL = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class Proposal:
    id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    summary: dict[str, Any]
    status: str
    created_turn_id: uuid.UUID
    expires_at: datetime


async def create(
    conn: AsyncConnection, ctx: ToolContext, kind: str, payload: dict[str, Any], summary: dict[str, Any],
    proposal_id: uuid.UUID,
) -> None:
    """Supersede the conversation's open proposals, then store this one (open, 15 min).

    A partial unique index allows one open proposal per conversation, so the UPDATE must
    come first; it also means a stale summary can never be confirmed after a newer one.
    """
    if ctx.conversation_id is None or ctx.turn_id is None:
        raise ValueError("proposals exist only inside a chat turn")
    await conn.execute(
        text(
            "UPDATE proposals SET status = 'superseded' "
            "WHERE conversation_id = :c AND user_id = :u AND status = 'open'"
        ),
        {"c": ctx.conversation_id, "u": ctx.user_id},
    )
    now = clock.now()
    await conn.execute(
        text(
            "INSERT INTO proposals (id, org_id, user_id, conversation_id, kind, payload, summary, "
            "created_turn_id, expires_at, created_at) VALUES (:id, :o, :u, :c, :k, CAST(:p AS jsonb), "
            "CAST(:s AS jsonb), :turn, :exp, :now)"
        ),
        {
            "id": proposal_id, "o": ctx.org_id, "u": ctx.user_id, "c": ctx.conversation_id, "k": kind,
            "p": json.dumps(payload), "s": json.dumps(summary), "turn": ctx.turn_id,
            "exp": now + PROPOSAL_TTL, "now": now,
        },
    )  # fmt: skip


async def lock(conn: AsyncConnection, ctx: ToolContext, proposal_id: uuid.UUID) -> Proposal | None:
    """The caller's proposal in this conversation, row-locked until the transaction ends.
    The lock serialises two confirms of the same proposal (a double click)."""
    row = (
        await conn.execute(
            text(
                "SELECT id, kind, payload, summary, status, created_turn_id, expires_at FROM proposals "
                "WHERE id = :p AND user_id = :u AND conversation_id = :c FOR UPDATE"
            ),
            {"p": proposal_id, "u": ctx.user_id, "c": ctx.conversation_id},
        )
    ).one_or_none()
    return Proposal(*row) if row else None


async def set_status(conn: AsyncConnection, proposal_id: uuid.UUID, status: str) -> None:
    await conn.execute(text("UPDATE proposals SET status = :s WHERE id = :p"), {"s": status, "p": proposal_id})
