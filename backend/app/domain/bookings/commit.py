"""Committers: carry out a confirmed booking proposal inside the confirm transaction.

Each re-checks live facts before writing anything:
  - create_booking: the service's price and the slot (slot engine). A new price means a
    fresh proposal and nothing committed. A slot that is no longer free means a refusal
    with the 3 nearest alternatives; if the practitioner was auto-assigned and another
    qualified one is free at that time, a fresh proposal naming them instead.
  - reschedule_booking: locks the booking (SELECT ... FOR UPDATE), re-checks that it is
    still confirmed, upcoming and unchanged, then re-checks the new slot.
  - cancel_booking: locks the booking, re-checks it, and recomputes the fee: if the
    24-hour line was crossed since the summary, a fresh proposal with the new fee.

The EXCLUDE constraints are the final word on conflicts: a race the slot check can't see
(two confirms in flight at once) ends in exclusion_violation. The write runs inside a
savepoint, so that error is caught without losing the transaction and the customer gets
alternatives. Concurrent inserts can also die with deadlock_detected: retried once, then
treated as a conflict.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import TypeVar

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from app import clock
from app.domain.bookings import repo, service
from app.domain.bookings.service import booking_number, provider_for
from app.domain.money import format_money, money_str
from app.domain.proposals.repo import Proposal
from app.domain.proposals.service import Committer, Describer
from app.domain.reply import Reply
from app.domain.slots import engine
from app.domain.slots.dates import local_iso
from app.domain.slots.model import Ignore
from app.domain.timefmt import local_display
from app.tenancy.context import ToolContext
from app.tenancy.orgs import Org, get_org_by_id

CHANGED = "CHANGED: something changed since the summary, so NOTHING was committed."
EXCLUSION_VIOLATION = "23P01"
DEADLOCK_DETECTED = "40P01"
T = TypeVar("T")


class SlotTaken(Exception):
    """The database refused the write: the slot overlaps a confirmed booking."""

    def __init__(self, constraint: str | None) -> None:
        super().__init__(constraint)
        self.constraint = constraint


def _pg_error(exc: DBAPIError) -> tuple[str | None, str | None]:
    """(sqlstate, constraint name) of the asyncpg error under SQLAlchemy's wrapper."""
    cause = getattr(exc.orig, "__cause__", None)
    code = getattr(cause, "sqlstate", None) or getattr(exc.orig, "sqlstate", None)
    return code, getattr(cause, "constraint_name", None)


async def guarded_write(conn: AsyncConnection, write: Callable[[], Awaitable[T]]) -> T:
    """Run `write` in a savepoint. exclusion_violation → SlotTaken; deadlock_detected →
    retry once, then SlotTaken. Rolling back to the savepoint undoes only the write, so the
    caller can still answer (and mark the proposal) in the same transaction."""
    for attempt in (1, 2):
        try:
            async with conn.begin_nested():
                return await write()
        except DBAPIError as exc:
            code, constraint = _pg_error(exc)
            if code == EXCLUSION_VIOLATION:
                raise SlotTaken(constraint) from None
            if code == DEADLOCK_DETECTED:
                if attempt == 1:
                    continue  # the other transaction has gone; the retry sees the winner
                raise SlotTaken(None) from None
            raise
    raise AssertionError("unreachable")


def _reason(taken: SlotTaken) -> str:
    return engine.CUSTOMER_BUSY if taken.constraint == "no_customer_overlap" else engine.PRACTITIONER_BUSY


async def _org(conn: AsyncConnection, ctx: ToolContext) -> Org:
    org = await get_org_by_id(conn, ctx.org_id)
    assert org is not None  # the session's org always exists (FK)
    return org


def _changed(fresh: Reply, changes: list[str]) -> Reply:
    """Wrap a re-quote. If the re-quote itself was refused, that refusal is the answer."""
    if fresh.status != "proposed":
        return fresh
    return Reply(f"{CHANGED} {' '.join(changes)} {fresh.text}", cards=fresh.cards, ok=fresh.ok, status="changed")


def _at(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


# ───────────── create ─────────────


async def commit_create(conn: AsyncConnection, ctx: ToolContext, proposal: Proposal) -> Reply:
    org = await _org(conn, ctx)
    p = proposal.payload
    start, end = _at(p["starts_at"]), _at(p["ends_at"])
    pid = uuid.UUID(p["practitioner_id"])
    svc = await repo.service_by_id(conn, uuid.UUID(p["service_id"]))
    if svc is None or pid not in svc.practitioners:
        return Reply(f"REFUSED: {p['service_name']} with {p['practitioner_name']} is no longer offered. Nothing was "
                     "booked. Offer list_services to choose again.", status="refused")  # fmt: skip
    asked_for = None if p["auto_practitioner"] else p["practitioner_name"]

    if svc.price != Decimal(p["price"]):
        changes = [f"The price of {svc.name} changed from {format_money(Decimal(p['price']), org.currency)} to "
                   f"{format_money(svc.price, org.currency)}."]  # fmt: skip
        fresh = await service.propose_create(conn, ctx, org, service=svc.name, start=local_iso(start, org.timezone),
                                             practitioner=asked_for, intro="Updated summary:", changes=changes)  # fmt: skip
        return _changed(fresh, changes)

    now = clock.now()
    reason = await provider_for(org).check(conn, ctx, svc, start, pid, now)
    if reason is None:
        try:
            number = await guarded_write(conn, lambda: repo.insert_booking(
                conn, ctx, service_id=svc.id, practitioner_id=pid, starts_at=start, ends_at=end,
                price=svc.price, proposal_id=proposal.id, now=now,
            ))  # fmt: skip
        except SlotTaken as taken:
            reason = _reason(taken)
    if reason is not None:
        if reason == engine.PRACTITIONER_BUSY and p["auto_practitioner"]:
            # We chose the practitioner; if someone else qualified is free then, offer them.
            changes = [f"{p['practitioner_name']} was booked by someone else in the meantime."]
            fresh = await service.propose_create(conn, ctx, org, service=svc.name, start=local_iso(start, org.timezone),
                                                 practitioner=None, intro="Updated summary:", changes=changes)  # fmt: skip
            if fresh.status == "proposed":
                return _changed(fresh, changes)
        return await service.refuse_slot(conn, ctx, org, svc, start, reason, practitioner=pid, now=now)

    shown = booking_number(org, number)
    card = {
        "type": "booking_confirmed", "booking_number": shown, "status": "confirmed", "service": svc.name,
        "practitioner": p["practitioner_name"], **service.slot_when(org, start),
        "duration_min": svc.duration_min, "price": money_str(svc.price), "price_display": format_money(svc.price, org.currency),
    }  # fmt: skip
    return Reply(
        f"CONFIRMED: booking {shown} is made: {svc.name} with {p['practitioner_name']} on {card['when']} "
        f"({org.timezone}), {card['price_display']} paid at the studio. Give the customer the booking number {shown}.",
        cards=[card], status="committed",
    )  # fmt: skip


# ───────────── reschedule ─────────────


async def commit_reschedule(conn: AsyncConnection, ctx: ToolContext, proposal: Proposal) -> Reply:
    org = await _org(conn, ctx)
    p = proposal.payload
    b = await repo.lock_own_booking_by_id(conn, ctx, uuid.UUID(p["booking_id"]))
    if b is None:
        return service.NOT_FOUND
    now = clock.now()
    if refusal := service.refuse_unchangeable(org, b, "rescheduled", now):
        return refusal
    old = p["old"]
    if (str(b.practitioner_id), b.starts_at, b.ends_at) != (old["practitioner_id"], _at(old["starts_at"]), _at(old["ends_at"])):
        return Reply(f"{CHANGED} Booking {p['booking_number']} was changed after this summary was made. Show the "
                     "customer list_my_bookings and propose again.", status="changed")  # fmt: skip
    svc = await repo.service_by_id(conn, b.service_id)
    assert svc is not None  # FK
    new = p["new"]
    pid, start, end = uuid.UUID(new["practitioner_id"]), _at(new["starts_at"]), _at(new["ends_at"])
    ignore = Ignore(b.practitioner_id, b.starts_at, b.ends_at)
    reason = await provider_for(org).check(conn, ctx, svc, start, pid, now, ignore=ignore)
    if reason is None:
        try:
            await guarded_write(conn, lambda: repo.move_booking(conn, b.id, practitioner_id=pid, starts_at=start, ends_at=end))
        except SlotTaken as taken:
            reason = _reason(taken)
    if reason is not None:
        return await service.refuse_slot(conn, ctx, org, svc, start, reason, practitioner=pid, booking=b, now=now)

    when = local_display(start, org.timezone)
    card = {
        "type": "booking_rescheduled", "booking_number": p["booking_number"], "status": "confirmed",
        "service": svc.name, "practitioner": new["practitioner_name"], "when": when,
        "old": {"practitioner": b.practitioner_name, "when": local_display(b.starts_at, org.timezone)},
    }  # fmt: skip
    return Reply(f"CONFIRMED: booking {p['booking_number']} is moved to {when} with {new['practitioner_name']} "
                 f"(was {card['old']['when']}).", cards=[card], status="committed")  # fmt: skip


# ───────────── cancel ─────────────


async def commit_cancel(conn: AsyncConnection, ctx: ToolContext, proposal: Proposal) -> Reply:
    org = await _org(conn, ctx)
    p = proposal.payload
    b = await repo.lock_own_booking_by_id(conn, ctx, uuid.UUID(p["booking_id"]))
    if b is None:
        return service.NOT_FOUND
    now = clock.now()
    if refusal := service.refuse_unchangeable(org, b, "cancelled", now):
        return refusal
    if b.starts_at != _at(p["starts_at"]):
        return Reply(f"{CHANGED} Booking {p['booking_number']} was moved after this summary was made. Propose the "
                     "cancellation again.", status="changed")  # fmt: skip
    fee = service.fee_for(b.price, b.starts_at, now)
    if fee != Decimal(p["fee"]):
        changes = [f"It is now within 24 hours of the appointment, so the cancellation fee is "
                   f"{format_money(fee, org.currency)} (was {format_money(Decimal(p['fee']), org.currency)})."]  # fmt: skip
        fresh = await service.propose_cancel_of(conn, ctx, org, b, intro="Updated summary:")
        return _changed(fresh, changes)
    await repo.cancel(conn, b.id, fee)
    card = {
        "type": "booking_cancelled", "booking_number": p["booking_number"], "status": "cancelled",
        "service": b.service_name, "practitioner": b.practitioner_name, "when": local_display(b.starts_at, org.timezone),
        "fee": money_str(fee), "fee_display": format_money(fee, org.currency),
    }  # fmt: skip
    fee_text = "No fee applies." if fee == 0 else f"A cancellation fee of {card['fee_display']} is recorded (pay at the studio)."
    return Reply(f"CONFIRMED: booking {p['booking_number']} is cancelled. {fee_text}", cards=[card], status="committed")


COMMITTERS: dict[str, Committer] = {
    "create_booking": commit_create,
    "reschedule_booking": commit_reschedule,
    "cancel_booking": commit_cancel,
}


async def describe(conn: AsyncConnection, ctx: ToolContext, proposal: Proposal) -> str | None:
    """The booking this confirmed proposal created (bookings.proposal_id) or changed
    (reschedule/cancel: payload booking_id), with its status as it is NOW."""
    booking_id = proposal.payload.get("booking_id")
    row = (await conn.execute(
        text("SELECT booking_number, status FROM bookings WHERE user_id = :u AND "
             + ("id = :i" if booking_id else "proposal_id = :p")),
        {"u": ctx.user_id, "i": uuid.UUID(booking_id) if booking_id else None, "p": proposal.id},
    )).one_or_none()  # fmt: skip
    if row is None:
        return None
    org = await _org(conn, ctx)
    return f"booking {booking_number(org, row.booking_number)} (status: {row.status})"


DESCRIBERS: dict[str, Describer] = dict.fromkeys(COMMITTERS, describe)
