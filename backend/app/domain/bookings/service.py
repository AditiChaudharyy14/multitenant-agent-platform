"""Booking operations behind the booking tools. Each runs inside the caller's tenant_tx and
returns a Reply: text for the model plus cards for the UI.

Mutations never change a booking here. They validate against live data with the slot
engine and store a proposal; app/domain/bookings/commit.py carries it out after the
customer confirms. A refused slot never makes a proposal: the customer gets the reason
and the 3 nearest free alternatives instead.

Booking numbers (LW-1001) are the only identifiers customers see.
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncConnection

from app import clock
from app.domain.bookings import repo
from app.domain.bookings.repo import Booking
from app.domain.money import format_money, money_str, to_money
from app.domain.numbers import display_number, parse_number
from app.domain.proposals import repo as proposals
from app.domain.proposals.repo import PROPOSAL_TTL
from app.domain.reply import Reply
from app.domain.slots import engine
from app.domain.slots.dates import Resolved, local_iso, parse_iso_date, parse_local_start, resolve_when
from app.domain.slots.model import Ignore, Schedule, Service, Slot
from app.domain.slots.provider import LocalSlotProvider, SlotProvider
from app.domain.timefmt import local_display
from app.tenancy.context import ToolContext
from app.tenancy.orgs import Org

# One text for every miss: a foreign booking and a nonexistent one must be indistinguishable.
NOT_FOUND = Reply(
    "NOT FOUND: no booking with that number exists on this customer's account. Ask them to "
    "check the number, or call list_my_bookings to see their bookings.",
    status="not_found",
)
CONFIRM_LABELS = {
    "create_booking": "Confirm booking", "reschedule_booking": "Confirm new time",
    "cancel_booking": "Confirm cancellation",
}  # fmt: skip
FEE_FREE_BEFORE = timedelta(hours=24)  # cancelling more than this far ahead is free
FEE_RATE = Decimal("0.50")  # otherwise half the booked price (mocked: recorded, not charged)
FEE_RULE = "Free more than 24 hours ahead; 50% of the price within 24 hours."
MAX_RANGE_DAYS = 14  # find_available_slots searches at most this many days at once
CARD_SLOTS_PER_DAY = 16  # the card shows at most this many buttons per day…
CARD_SLOTS_TOTAL = 40  # …and this many in all; the model's text covers every free time


def provider_for(org: Org) -> SlotProvider:
    """The one place that chooses the slot provider. Swap in another implementation here."""
    return LocalSlotProvider(org.timezone)


# ───────────── small formatters ─────────────


def _day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b %Y}"


def _date(dt: datetime, tz: str) -> str:
    return _day(dt.astimezone(ZoneInfo(tz)).date())


def _time(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%H:%M")


def _hhmm(t: Any) -> str:
    return t.strftime("%H:%M")


def booking_number(org: Org, n: int) -> str:
    return display_number(org.booking_prefix, n)


def fee_for(price: Decimal, starts_at: datetime, now: datetime) -> Decimal:
    """Cancellation fee: 0 when more than 24 hours ahead, else 50% of the booked price."""
    return to_money(0) if starts_at - now > FEE_FREE_BEFORE else to_money(price * FEE_RATE)


def slot_when(org: Org, start: datetime) -> dict[str, str]:
    return {"date": _date(start, org.timezone), "time": _time(start, org.timezone),
            "when": local_display(start, org.timezone)}  # fmt: skip


def slot_card(
    org: Org, service: Service, slot: Slot, *, practitioner: str | None, booking: str | None = None,
    practitioners: list[str] | None = None,
) -> dict[str, Any]:  # fmt: skip
    """One clickable slot. The button sends exactly what create_booking (or, with a booking
    number, reschedule_booking) needs; the server re-validates everything anyway."""
    start = local_iso(slot.start, org.timezone)
    action: dict[str, Any] = {"type": "select_slot", "service": service.name, "practitioner": practitioner, "start": start}
    if booking:
        action["booking_number"] = booking
    card = {"start": start, "display": local_display(slot.start, org.timezone), "practitioner": practitioner,
            "action": action}  # fmt: skip
    if practitioners is not None:
        card["practitioners"] = practitioners
    return card


# ───────────── resolution helpers ─────────────


def _pick(raw: str, names: list[str], what: str) -> str | Reply:
    found = repo.match_name(raw, names)
    if len(found) == 1:
        return found[0]
    listing = "; ".join(names)
    if not found:
        return Reply(f"ERROR: no {what} called {raw!r}. The {what}s are: {listing}. Never guess; ask the customer "
                     "if unsure.", ok=False, status="error")  # fmt: skip
    return Reply(f"ERROR: {raw!r} matches several {what}s: {'; '.join(found)}. Ask the customer which one.",
                 ok=False, status="error")  # fmt: skip


async def resolve_service(conn: AsyncConnection, raw: str) -> Service | Reply:
    services = await repo.services(conn)
    name = _pick(raw, [s.name for s in services], "service")
    return name if isinstance(name, Reply) else next(s for s in services if s.name == name)


def resolve_practitioner(service: Service, raw: str | None, all_practitioners: dict[uuid.UUID, str]) -> uuid.UUID | None | Reply:
    """None when no practitioner was asked for; a Reply when the name is unknown, ambiguous
    or not qualified for this service."""
    if not raw or not raw.strip():
        return None
    name = _pick(raw, sorted(all_practitioners.values()), "practitioner")
    if isinstance(name, Reply):
        return name
    pid = next(p for p, n in all_practitioners.items() if n == name)
    if pid not in service.practitioners:
        who = ", ".join(sorted(service.practitioners.values())) or "nobody at the moment"
        return Reply(f"REFUSED: {name} doesn't offer {service.name}. It is offered by: {who}. Ask the customer "
                     "which of them they'd like.", status="refused")  # fmt: skip
    return pid


def reason_text(org: Org, schedule: Schedule, service: Service, start: datetime, reason: str, who: str | None) -> str:
    """A refusal reason in words the customer can act on."""
    tz = ZoneInfo(org.timezone)
    local = start.astimezone(tz)
    day = local.date()
    hours = schedule.hours.get(day.weekday())
    span = f"{_hhmm(hours[0])}–{_hhmm(hours[1])}" if hours else ""
    match reason:
        case engine.HOLIDAY:
            return f"{_day(day)} is a holiday ({schedule.holiday_names.get(day, 'studio holiday')}), so the studio is closed"
        case engine.CLOSED:
            return f"the studio is closed on {local:%A}s, so {_day(day)} can't be booked"
        case engine.NOT_ON_STEP:
            return f"appointments start in 15-minute steps (:00, :15, :30, :45); {local:%H:%M} isn't one"
        case engine.OUTSIDE_HOURS:
            return f"{local:%H:%M} is outside opening hours on {_day(day)} ({span})"
        case engine.OVERFLOWS_CLOSING:
            return (f"a {service.duration_min}-minute {service.name} starting at {local:%H:%M} would end after "
                    f"closing time ({_hhmm(hours[1]) if hours else ''}) on {_day(day)}")  # fmt: skip
        case engine.TOO_SOON:
            return "appointments must start at least 60 minutes from now, and that time has passed or is too close"
        case engine.PRACTITIONER_BUSY:
            return f"{who or 'no qualified practitioner'} is already booked at that time"
        case engine.CUSTOMER_BUSY:
            return "they already have a booking that overlaps that time (one appointment at a time)"
    return reason


async def refuse_slot(
    conn: AsyncConnection, ctx: ToolContext, org: Org, service: Service, start: datetime, reason: str, *,
    practitioner: uuid.UUID | None, booking: Booking | None = None, now: datetime | None = None,
) -> Reply:  # fmt: skip
    """REFUSED + the reason + the 3 nearest free alternatives (that practitioner first). No proposal."""
    now = now or clock.now()
    provider = provider_for(org)
    schedule = await provider.schedule(conn)
    ignore = Ignore(booking.practitioner_id, booking.starts_at, booking.ends_at) if booking else None
    alts = await provider.nearest_alternatives(conn, ctx, service, start, now, practitioner=practitioner, ignore=ignore)
    who = service.practitioners.get(practitioner) if practitioner else None
    number = booking_number(org, booking.booking_number) if booking else None
    why = reason_text(org, schedule, service, start, reason, who)
    listed = "; ".join(f"{local_display(a.start, org.timezone)} with {a.practitioner_name}" for a in alts)
    again = f"reschedule_booking for {number}" if booking else "create_booking"
    card = {
        "type": "slot_alternatives", "service": service.name, "reason": why, "requested": local_display(start, org.timezone),
        "booking_number": number,
        "slots": [slot_card(org, service, a, practitioner=a.practitioner_name, booking=number) for a in alts],
    }  # fmt: skip
    return Reply(
        f"REFUSED: {why}. NO PROPOSAL was made. "
        + (f"The {len(alts)} nearest free times: {listed}. Offer these (they are buttons on the card) and call "
           f"{again} again once the customer picks one." if alts else "There is no free time within two weeks of it.")
        ,
        cards=[card], status="refused",
    )  # fmt: skip


async def _propose(
    conn: AsyncConnection, ctx: ToolContext, org: Org, kind: str, payload: dict[str, Any], card: dict[str, Any], what: str,
) -> Reply:  # fmt: skip
    """Store the proposal and build its booking_summary card (with the Confirm button)."""
    pid = uuid.uuid4()
    card = {
        "type": "booking_summary", "kind": kind, "proposal_id": str(pid), **card,
        "expires_at": local_display(clock.now() + PROPOSAL_TTL, org.timezone),
        "actions": [{"type": "confirm_proposal", "proposal_id": str(pid), "label": CONFIRM_LABELS[kind]}],
    }  # fmt: skip
    await proposals.create(conn, ctx, kind, payload, card, pid)
    return Reply(
        f"PROPOSED, NOT DONE YET: {what} Show this summary and ask the customer to confirm (Confirm button, or a "
        f"reply like 'yes'). It expires in 15 minutes. proposal_id={pid} (internal: pass it to confirm_proposal, "
        "never show it).",
        cards=[card], status="proposed",
    )  # fmt: skip


async def _own(conn: AsyncConnection, ctx: ToolContext, org: Org, raw: str) -> Booking | None:
    n = parse_number(raw, org.booking_prefix or "")
    return await repo.own_booking(conn, ctx, n) if n else None


def refuse_unchangeable(org: Org, b: Booking, action: str, now: datetime) -> Reply | None:
    """Only confirmed bookings that haven't started can be rescheduled or cancelled."""
    number = booking_number(org, b.booking_number)
    if b.status != "confirmed":
        return Reply(f"REFUSED: booking {number} is {b.status}, so it can't be {action}. Only confirmed upcoming "
                     "bookings can be.", status="refused")  # fmt: skip
    if b.starts_at <= now:
        return Reply(f"REFUSED: booking {number} was for {local_display(b.starts_at, org.timezone)}, which has "
                     f"already started or passed, so it can't be {action}.", status="refused")  # fmt: skip
    return None


# ───────────── read-only: services, hours, slots, my bookings ─────────────


async def list_services(conn: AsyncConnection, org: Org) -> Reply:
    services = await repo.services(conn)
    cards = [
        {"name": s.name, "duration_min": s.duration_min, "price": money_str(s.price),
         "price_display": format_money(s.price, org.currency), "practitioners": sorted(s.practitioners.values())}
        for s in services
    ]  # fmt: skip
    rows = [f"{c['name']} | {c['duration_min']} min | {c['price_display']} | {', '.join(c['practitioners'])}" for c in cards]
    return Reply("LIVE services (name | duration | price | practitioners):\n" + "\n".join(rows),
                 cards=[{"type": "service_list", "services": cards}])  # fmt: skip


async def opening_hours(conn: AsyncConnection, org: Org) -> Reply:
    schedule = await provider_for(org).schedule(conn)
    today = clock.now().astimezone(ZoneInfo(org.timezone)).date()
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    week = []
    for wd, name in enumerate(names):
        hours = schedule.hours.get(wd)
        week.append({"day": name, "closed": hours is None,
                     "opens": _hhmm(hours[0]) if hours else None, "closes": _hhmm(hours[1]) if hours else None})  # fmt: skip
    upcoming = sorted(d for d in schedule.holidays if today <= d <= today + timedelta(days=90))
    holidays = [{"date": _day(d), "name": schedule.holiday_names.get(d, "")} for d in upcoming]
    lines = [f"{w['day']}: closed" if w["closed"] else f"{w['day']}: {w['opens']}–{w['closes']}" for w in week]
    text = f"Opening hours ({org.timezone}):\n" + "\n".join(lines)
    text += "\nUpcoming holidays (closed): " + ("; ".join(f"{h['date']} ({h['name']})" for h in holidays) or "none")
    return Reply(text, cards=[{"type": "opening_hours", "timezone": org.timezone, "week": week, "holidays": holidays}])


def _runs(starts: list[datetime], tz: str) -> str:
    """'09:00–11:45, 14:00' — consecutive 15-minute starts collapsed into ranges."""
    out: list[str] = []
    run: list[datetime] = []
    for s in starts:
        if run and s - run[-1] != timedelta(minutes=15):
            out.append(_time(run[0], tz) + (f"–{_time(run[-1], tz)}" if len(run) > 1 else ""))
            run = []
        run.append(s)
    if run:
        out.append(_time(run[0], tz) + (f"–{_time(run[-1], tz)}" if len(run) > 1 else ""))
    return ", ".join(out)


def _range_days(date_from: str | None, date_to: str | None, today: date) -> list[date] | Reply:
    try:
        first = parse_iso_date(date_from) if date_from else today
        last = parse_iso_date(date_to) if date_to else first + timedelta(days=6)
    except ValueError as exc:
        return Reply(f"ERROR: {exc}. Use YYYY-MM-DD, or pass the customer's words as `when`.", ok=False, status="error")
    if last < today:
        return Reply(f"REFUSED: {_day(last)} is in the past; bookings can only be made for today or later.", status="refused")
    first = max(first, today)
    if last < first:
        return Reply("ERROR: date_to is before date_from.", ok=False, status="error")
    if (last - first).days >= MAX_RANGE_DAYS:
        last = first + timedelta(days=MAX_RANGE_DAYS - 1)
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


async def find_slots(
    conn: AsyncConnection, ctx: ToolContext, org: Org, *, service: str, when: str | None,
    date_from: str | None, date_to: str | None, practitioner: str | None,
) -> Reply:  # fmt: skip
    found = await resolve_service(conn, service)
    if isinstance(found, Reply):
        return found
    svc = found
    pid = resolve_practitioner(svc, practitioner, await repo.practitioners(conn))
    if isinstance(pid, Reply):
        return pid
    now = clock.now()
    today = now.astimezone(ZoneInfo(org.timezone)).date()
    window = None
    if when and when.strip():
        r: Resolved = resolve_when(when, now, org.timezone)
        if r.error:
            return Reply(f"REFUSED: {r.error}" if "past" in r.error else f"ERROR: {r.error}",
                         ok="past" in r.error, status="need_date")  # fmt: skip
        if r.ambiguous:
            options = " or ".join(_day(d) for d in r.candidates)
            return Reply(f"AMBIGUOUS: {when!r} could mean {options}. Ask the customer which one they mean, then "
                         "call find_available_slots again with that date.", status="ambiguous")  # fmt: skip
        days, window = r.days, r.window
    else:
        days = _range_days(date_from, date_to, today)
        if isinstance(days, Reply):
            return days
    provider = provider_for(org)
    schedule = await provider.schedule(conn)
    slots = await provider.find(conn, ctx, svc, days, now, practitioner=pid, window=window)

    by_day: dict[date, list[Slot]] = {}
    for s in slots:
        by_day.setdefault(s.start.astimezone(ZoneInfo(org.timezone)).date(), []).append(s)
    lines, card_slots, notes = [], [], []
    for d in days:
        if why := engine.day_rule(schedule, d):
            note = (f"{_day(d)}: holiday ({schedule.holiday_names.get(d, '')}), closed" if why == engine.HOLIDAY
                    else f"{_day(d)}: closed")  # fmt: skip
            notes.append(note)
            lines.append(note)
            continue
        day_slots = by_day.get(d, [])
        starts = sorted({s.start for s in day_slots})
        if not starts:
            lines.append(f"{_day(d)}: fully booked" + (" in that part of the day" if window else ""))
            continue
        lines.append(f"{_day(d)}: {_runs(starts, org.timezone)}")
        for start in starts[:CARD_SLOTS_PER_DAY]:
            at = [s for s in day_slots if s.start == start]
            if len(card_slots) < CARD_SLOTS_TOTAL:
                if pid:
                    card_slots.append(slot_card(org, svc, at[0], practitioner=at[0].practitioner_name))
                else:
                    card_slots.append(slot_card(org, svc, at[0], practitioner=None,
                                                practitioners=[s.practitioner_name for s in at]))  # fmt: skip
    who = svc.practitioners[pid] if pid else "any of: " + ", ".join(sorted(svc.practitioners.values()))
    span = f", {_hhmm(window[0])}–{'closing' if window[1].hour == 23 else _hhmm(window[1])}" if window else ""
    card = {
        "type": "slot_list", "service": svc.name, "duration_min": svc.duration_min,
        "price_display": format_money(svc.price, org.currency), "practitioner": svc.practitioners[pid] if pid else None,
        "resolved_dates": [_day(d) for d in days], "window": span.lstrip(", ") or None,
        "slots": card_slots, "notes": notes,
    }  # fmt: skip
    return Reply(
        f"LIVE free start times for {svc.name} ({svc.duration_min} min, {card['price_display']}) with {who}{span}, "
        f"{org.timezone} time (each start fits the full duration before closing):\n" + "\n".join(lines)
        + "\nThe card shows them as buttons. To book one, call create_booking with start as YYYY-MM-DDTHH:MM.",
        cards=[card],
    )  # fmt: skip


def _booking_card(org: Org, b: Booking) -> dict[str, Any]:
    return {
        "booking_number": booking_number(org, b.booking_number), "service": b.service_name,
        "practitioner": b.practitioner_name, **slot_when(org, b.starts_at), "status": b.status,
        "price_display": format_money(b.price, org.currency),
        "cancellation_fee_display": format_money(b.cancellation_fee, org.currency) if b.cancellation_fee is not None else None,
    }  # fmt: skip


async def booking_groups(conn: AsyncConnection, ctx: ToolContext, org: Org) -> dict[str, list[dict[str, Any]]]:
    """The caller's bookings as display cards: upcoming (soonest first), then past and
    cancelled (latest first). The ONE source for both the list_my_bookings tool and the
    My bookings panel (GET /api/bookings), so they can't disagree."""
    now = clock.now()
    mine = await repo.list_own(conn, ctx)
    upcoming = [b for b in mine if b.status == "confirmed" and b.starts_at > now]
    past = sorted((b for b in mine if b not in upcoming), key=lambda b: b.starts_at, reverse=True)
    return {"upcoming": [_booking_card(org, b) for b in upcoming], "past": [_booking_card(org, b) for b in past]}


async def list_bookings(conn: AsyncConnection, ctx: ToolContext, org: Org) -> Reply:
    groups = await booking_groups(conn, ctx, org)

    def line(c: dict[str, Any]) -> str:
        fee = f", cancellation fee {c['cancellation_fee_display']}" if c["status"] == "cancelled" and c["cancellation_fee_display"] else ""
        return f"{c['booking_number']} | {c['when']} | {c['service']} with {c['practitioner']} | {c['status']}{fee}"

    text = "Upcoming:\n" + ("\n".join(line(c) for c in groups["upcoming"]) or "none")
    text += "\nPast and cancelled:\n" + ("\n".join(line(c) for c in groups["past"]) or "none")
    return Reply(text, cards=[{"type": "booking_list", **groups}])


def _parse_start(raw: str, org: Org) -> datetime | Reply:
    try:
        return parse_local_start(raw, org.timezone)
    except ValueError as exc:
        return Reply(f"ERROR: {exc}. Use a start time from find_available_slots, as YYYY-MM-DDTHH:MM in "
                     f"{org.timezone} time.", ok=False, status="error")  # fmt: skip


async def propose_create(
    conn: AsyncConnection, ctx: ToolContext, org: Org, *, service: str, start: str, practitioner: str | None,
    intro: str = "", changes: list[str] | None = None,
) -> Reply:  # fmt: skip
    found = await resolve_service(conn, service)
    if isinstance(found, Reply):
        return found
    svc = found
    pid = resolve_practitioner(svc, practitioner, await repo.practitioners(conn))
    if isinstance(pid, Reply):
        return pid
    at = _parse_start(start, org)
    if isinstance(at, Reply):
        return at
    now = clock.now()
    provider = provider_for(org)
    auto = pid is None
    if auto:
        # No preference: the first qualified practitioner (by name) who is free then.
        reason = engine.PRACTITIONER_BUSY
        for candidate in sorted(svc.practitioners, key=lambda p: svc.practitioners[p]):
            reason = await provider.check(conn, ctx, svc, at, candidate, now)
            if reason is None:
                pid = candidate
                break
            if reason != engine.PRACTITIONER_BUSY:
                break  # a calendar or customer reason is the same for everyone
        if pid is None:
            return await refuse_slot(conn, ctx, org, svc, at, reason, practitioner=None, now=now)
    else:
        reason = await provider.check(conn, ctx, svc, at, pid, now)
        if reason:
            return await refuse_slot(conn, ctx, org, svc, at, reason, practitioner=pid, now=now)

    end = at + timedelta(minutes=svc.duration_min)
    name = svc.practitioners[pid]
    payload = {
        "service_id": str(svc.id), "service_name": svc.name, "practitioner_id": str(pid), "practitioner_name": name,
        "auto_practitioner": auto, "starts_at": at.isoformat(), "ends_at": end.isoformat(),
        "duration_min": svc.duration_min, "price": money_str(svc.price),
    }  # fmt: skip
    card = {
        "service": svc.name, "practitioner": name, **slot_when(org, at), "duration_min": svc.duration_min,
        "price": money_str(svc.price), "price_display": format_money(svc.price, org.currency),
        "practitioner_auto_assigned": auto, "changes": changes or [],
    }  # fmt: skip
    what = (f"{intro + ' ' if intro else ''}{svc.name} ({svc.duration_min} min) with {name}"
            f"{' (assigned: the customer named no practitioner, so tell them who)' if auto else ''} on "
            f"{card['when']} ({org.timezone}); price {card['price_display']}, paid at the studio.")  # fmt: skip
    if changes:
        what += " Changes: " + " ".join(changes)
    return await _propose(conn, ctx, org, "create_booking", payload, card, what)


async def propose_reschedule(
    conn: AsyncConnection, ctx: ToolContext, org: Org, *, number: str, new_start: str, practitioner: str | None,
) -> Reply:  # fmt: skip
    b = await _own(conn, ctx, org, number)
    if b is None:
        return NOT_FOUND
    now = clock.now()
    if refusal := refuse_unchangeable(org, b, "rescheduled", now):
        return refusal
    svc = await repo.service_by_id(conn, b.service_id)
    assert svc is not None  # bookings.service_id is a FK
    pid = resolve_practitioner(svc, practitioner, await repo.practitioners(conn))
    if isinstance(pid, Reply):
        return pid
    pid = pid or b.practitioner_id  # no new name: same practitioner
    at = _parse_start(new_start, org)
    if isinstance(at, Reply):
        return at
    if at == b.starts_at and pid == b.practitioner_id:
        return Reply(f"NOTHING TO CHANGE: booking {booking_number(org, b.booking_number)} is already at that time "
                     "with that practitioner.", ok=False, status="error")  # fmt: skip
    ignore = Ignore(b.practitioner_id, b.starts_at, b.ends_at)
    if pid not in svc.practitioners:  # their practitioner stopped offering it
        return await refuse_slot(conn, ctx, org, svc, at, engine.PRACTITIONER_BUSY, practitioner=None, booking=b, now=now)
    reason = await provider_for(org).check(conn, ctx, svc, at, pid, now, ignore=ignore)
    if reason:
        return await refuse_slot(conn, ctx, org, svc, at, reason, practitioner=pid, booking=b, now=now)
    end = at + timedelta(minutes=svc.duration_min)
    number_shown = booking_number(org, b.booking_number)
    name = svc.practitioners[pid]
    payload = {
        "booking_id": str(b.id), "booking_number": number_shown,
        "old": {"practitioner_id": str(b.practitioner_id), "starts_at": b.starts_at.isoformat(), "ends_at": b.ends_at.isoformat()},
        "new": {"practitioner_id": str(pid), "practitioner_name": name, "starts_at": at.isoformat(), "ends_at": end.isoformat()},
        "service_id": str(svc.id),
    }  # fmt: skip
    card = {
        "booking_number": number_shown, "service": svc.name, "practitioner": name, **slot_when(org, at),
        "duration_min": svc.duration_min, "price": money_str(b.price), "price_display": format_money(b.price, org.currency),
        "old": {"practitioner": b.practitioner_name, **slot_when(org, b.starts_at)},
    }  # fmt: skip
    what = (f"move booking {number_shown} ({svc.name}) from {card['old']['when']} with {b.practitioner_name} to "
            f"{card['when']} with {name}. The price stays {card['price_display']}.")  # fmt: skip
    return await _propose(conn, ctx, org, "reschedule_booking", payload, card, what)


async def propose_cancel(conn: AsyncConnection, ctx: ToolContext, org: Org, *, number: str, intro: str = "") -> Reply:
    b = await _own(conn, ctx, org, number)
    if b is None:
        return NOT_FOUND
    return await propose_cancel_of(conn, ctx, org, b, intro=intro)


async def propose_cancel_of(conn: AsyncConnection, ctx: ToolContext, org: Org, b: Booking, *, intro: str = "") -> Reply:
    now = clock.now()
    if refusal := refuse_unchangeable(org, b, "cancelled", now):
        return refusal
    fee = fee_for(b.price, b.starts_at, now)
    number_shown = booking_number(org, b.booking_number)
    payload = {"booking_id": str(b.id), "booking_number": number_shown, "starts_at": b.starts_at.isoformat(),
               "fee": money_str(fee)}  # fmt: skip
    card = {
        "booking_number": number_shown, "service": b.service_name, "practitioner": b.practitioner_name,
        **slot_when(org, b.starts_at), "price": money_str(b.price), "price_display": format_money(b.price, org.currency),
        "fee": money_str(fee), "fee_display": format_money(fee, org.currency), "fee_rule": FEE_RULE,
    }  # fmt: skip
    why = ("free, because it is more than 24 hours ahead" if fee == 0
           else "50% of the price, because it is within 24 hours of the appointment")  # fmt: skip
    what = (f"{intro + ' ' if intro else ''}cancel booking {number_shown} ({b.service_name} with "
            f"{b.practitioner_name} on {card['when']}). Cancellation fee: {card['fee_display']} ({why}).")  # fmt: skip
    return await _propose(conn, ctx, org, "cancel_booking", payload, card, what)
