"""Helpers for the booking tests, in the style of store_support.py: call tool handlers with
a server-built ToolContext, freeze the clock, and read the database back as the owner.

Times: NOW is Thu 5 Nov 2026, 14:00 in Asia/Kathmandu (08:15 UTC). local() builds an
org-local wall-clock time as an aware datetime; the slot engine works in UTC underneath.
"""

import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.tools.base import ToolResult
from app.tenancy.context import ToolContext
from tests.factories import Studio, set_owner_tenant
from tests.store_support import call, card  # noqa: F401  (re-exported for the booking tests)

TZ = ZoneInfo("Asia/Kathmandu")


def local(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


NOW = local(2026, 11, 5, 14, 0)  # Thursday


def iso(dt: datetime) -> str:
    """What the model passes as `start`: org-local, no offset, e.g. 2026-11-10T14:00."""
    return dt.astimezone(TZ).strftime("%Y-%m-%dT%H:%M")


def turn_ctx(studio: Studio, user: uuid.UUID, turn: uuid.UUID | None = None) -> ToolContext:
    return ToolContext(
        org_id=studio.org_id, user_id=user, role="customer",
        conversation_id=studio.conversations[user], turn_id=turn or uuid.uuid4(),
    )  # fmt: skip


def summary(result: ToolResult) -> dict[str, Any]:
    return card(result, "booking_summary")


def proposal_id(result: ToolResult) -> str:
    return summary(result)["proposal_id"]


def slot_starts(result: ToolResult, kind: str = "slot_list") -> list[str]:
    """The `start` of every slot in the result's slot card (org-local ISO, as the action sends)."""
    return [s["start"] for s in card(result, kind)["slots"]]


async def rows(owner: AsyncEngine, studio: Studio, sql: str, **params: Any) -> list[Any]:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, studio.org_id)
        return list((await conn.execute(text(sql), {"o": studio.org_id, **params})).all())


async def scalar(owner: AsyncEngine, studio: Studio, sql: str, **params: Any) -> Any:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, studio.org_id)
        return await conn.scalar(text(sql), {"o": studio.org_id, **params})


async def booking(owner: AsyncEngine, studio: Studio, number: int) -> Any:
    found = await rows(
        owner, studio,
        "SELECT b.*, p.name AS practitioner_name FROM bookings b JOIN practitioners p ON p.id = b.practitioner_id "
        "WHERE b.org_id = :o AND b.booking_number = :n",
        n=number,
    )  # fmt: skip
    return found[0] if found else None


async def set_service_price(owner: AsyncEngine, studio: Studio, key: str, price: str) -> None:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, studio.org_id)
        await conn.execute(
            text("UPDATE services SET price = CAST(:p AS numeric) WHERE org_id = :o AND id = :s"),
            {"o": studio.org_id, "s": studio.services[key], "p": price},
        )


async def db_snapshot(owner: AsyncEngine, studio: Studio) -> dict[str, list]:
    """Everything a booking tool could change. Equal snapshots mean nothing was written."""
    return {
        "bookings": await rows(owner, studio, "SELECT id, booking_number, practitioner_id, starts_at, ends_at, "
                                              "status, price, cancellation_fee FROM bookings WHERE org_id = :o ORDER BY id"),
        "proposals": await rows(owner, studio, "SELECT id, status FROM proposals WHERE org_id = :o ORDER BY id"),
    }  # fmt: skip
