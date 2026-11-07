"""Booking, service and practitioner SQL, always inside the caller's tenant_tx.

RLS already scopes everything to the org (and a customer to their own bookings). Queries
on bookings also filter on user_id = ctx.user_id explicitly, so a policy mistake alone
can't leak another customer's booking.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.slots.model import Service
from app.tenancy.context import ToolContext


@dataclass(frozen=True, slots=True)
class Booking:
    id: uuid.UUID
    booking_number: int
    service_id: uuid.UUID
    service_name: str
    practitioner_id: uuid.UUID
    practitioner_name: str
    starts_at: datetime
    ends_at: datetime
    status: str
    price: Decimal  # snapshot taken when booked
    cancellation_fee: Decimal | None


_BOOKING = (
    "b.id, b.booking_number, b.service_id, s.name, b.practitioner_id, p.name, b.starts_at, b.ends_at, "
    "b.status, b.price, b.cancellation_fee FROM bookings b JOIN services s ON s.id = b.service_id "
    "JOIN practitioners p ON p.id = b.practitioner_id"
)


# ───────────── catalogue ─────────────


async def services(conn: AsyncConnection) -> list[Service]:
    """Every service with its qualified practitioners, by name."""
    rows = (
        await conn.execute(
            text(
                "SELECT s.id, s.name, s.duration_min, s.price, p.id, p.name FROM services s "
                "LEFT JOIN practitioner_services ps ON ps.service_id = s.id "
                "LEFT JOIN practitioners p ON p.id = ps.practitioner_id ORDER BY s.name, p.name"
            )
        )
    ).all()
    found: dict[uuid.UUID, tuple[str, int, Decimal, dict[uuid.UUID, str]]] = {}
    for sid, name, minutes, price, pid, pname in rows:
        entry = found.setdefault(sid, (name, minutes, price, {}))
        if pid is not None:
            entry[3][pid] = pname
    return [Service(sid, n, m, p, who) for sid, (n, m, p, who) in found.items()]


async def service_by_id(conn: AsyncConnection, service_id: uuid.UUID) -> Service | None:
    return next((s for s in await services(conn) if s.id == service_id), None)


async def practitioners(conn: AsyncConnection) -> dict[uuid.UUID, str]:
    return dict((await conn.execute(text("SELECT id, name FROM practitioners ORDER BY name"))).all())


def _norm(name: str) -> str:
    """Lowercase words without punctuation or titles: 'Dr. Prakash Thapa' → 'prakash thapa'."""
    words = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split()
    return " ".join(w for w in words if w not in {"dr", "the", "with", "a", "an"})


def match_name(raw: str, names: list[str]) -> list[str]:
    """Names matching `raw`: an exact (normalised) match wins; otherwise every name that
    contains all of its words (so "swedish massage" finds "Swedish Relaxation Massage" and
    "asha" finds "Asha Gurung"). An empty list or several mean the caller must ask."""
    want = _norm(raw)
    if not want:
        return []
    exact = [n for n in names if _norm(n) == want]
    if exact:
        return exact
    words = set(want.split())
    return [n for n in names if words <= set(_norm(n).split())]


# ───────────── bookings ─────────────


async def own_booking(conn: AsyncConnection, ctx: ToolContext, number: int, *, lock: bool = False) -> Booking | None:
    sql = f"SELECT {_BOOKING} WHERE b.org_id = :o AND b.user_id = :u AND b.booking_number = :n"
    row = (
        await conn.execute(text(sql + (" FOR UPDATE OF b" if lock else "")), {"o": ctx.org_id, "u": ctx.user_id, "n": number})
    ).one_or_none()
    return Booking(*row) if row else None


async def lock_own_booking_by_id(conn: AsyncConnection, ctx: ToolContext, booking_id: uuid.UUID) -> Booking | None:
    row = (
        await conn.execute(
            text(f"SELECT {_BOOKING} WHERE b.id = :id AND b.org_id = :o AND b.user_id = :u FOR UPDATE OF b"),
            {"id": booking_id, "o": ctx.org_id, "u": ctx.user_id},
        )
    ).one_or_none()
    return Booking(*row) if row else None


async def list_own(conn: AsyncConnection, ctx: ToolContext) -> list[Booking]:
    rows = await conn.execute(
        text(f"SELECT {_BOOKING} WHERE b.org_id = :o AND b.user_id = :u ORDER BY b.starts_at"),
        {"o": ctx.org_id, "u": ctx.user_id},
    )
    return [Booking(*r) for r in rows.all()]


async def insert_booking(
    conn: AsyncConnection, ctx: ToolContext, *, service_id: uuid.UUID, practitioner_id: uuid.UUID,
    starts_at: datetime, ends_at: datetime, price: Decimal, proposal_id: uuid.UUID, now: datetime,
) -> int:  # fmt: skip
    """A new confirmed booking; the trigger assigns its number. The EXCLUDE constraints may
    reject it (exclusion_violation): the caller handles that."""
    return await conn.scalar(
        text(
            "INSERT INTO bookings (org_id, user_id, service_id, practitioner_id, starts_at, ends_at, status, price, "
            "conversation_id, proposal_id, created_at) VALUES (:o, :u, :s, :p, :start, :end, 'confirmed', :price, "
            ":c, :proposal, :now) RETURNING booking_number"
        ),
        {"o": ctx.org_id, "u": ctx.user_id, "s": service_id, "p": practitioner_id, "start": starts_at,
         "end": ends_at, "price": price, "c": ctx.conversation_id, "proposal": proposal_id, "now": now},
    )  # fmt: skip


async def move_booking(
    conn: AsyncConnection, booking_id: uuid.UUID, *, practitioner_id: uuid.UUID, starts_at: datetime, ends_at: datetime,
) -> None:  # fmt: skip
    await conn.execute(
        # A new time needs its own 24 h reminder (domain/notifications/reminders.py).
        text("UPDATE bookings SET practitioner_id = :p, starts_at = :s, ends_at = :e, reminder_sent_at = NULL WHERE id = :id"),
        {"p": practitioner_id, "s": starts_at, "e": ends_at, "id": booking_id},
    )


async def cancel(conn: AsyncConnection, booking_id: uuid.UUID, fee: Decimal) -> None:
    await conn.execute(
        text("UPDATE bookings SET status = 'cancelled', cancellation_fee = :f WHERE id = :id"),
        {"f": fee, "id": booking_id},
    )
