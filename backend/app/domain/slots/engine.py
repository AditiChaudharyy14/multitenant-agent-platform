"""The slot rules, as pure functions. Nothing here reads the database or the clock: the
caller passes "now" and the busy intervals, so the rules are deterministic and testable.

A start is bookable when ALL of these hold:
  1. its day is open (has opening hours) and is not a holiday;
  2. it is on a 15-minute step of the wall clock (:00, :15, :30, :45);
  3. it starts at or after opening, and start + the FULL service duration ends by closing
     on the same day;
  4. it starts at least 60 minutes after now;
  5. it overlaps none of the practitioner's confirmed bookings;
  6. it overlaps none of the customer's own confirmed bookings (with anyone).

ASSUMPTION: practitioners work the studio's full opening hours. The schema has no
per-practitioner rosters, so "the studio is open" and "the practitioner is working" are
the same thing here. A roster would add one more busy set per practitioner.
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.slots.model import LEAD, STEP, Interval, Schedule, Slot

# Refusal reasons. Values are stable machine codes; bookings/service.py words them.
CLOSED = "closed"
HOLIDAY = "holiday"
NOT_ON_STEP = "not_on_step"
OUTSIDE_HOURS = "outside_hours"
OVERFLOWS_CLOSING = "overflows_closing"
TOO_SOON = "too_soon"
PRACTITIONER_BUSY = "practitioner_busy"
CUSTOMER_BUSY = "customer_busy"


def _local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz)


def day_rule(schedule: Schedule, day: date) -> str | None:
    """Why `day` (org-local) can't take bookings at all, or None if it is open."""
    if day in schedule.holidays:
        return HOLIDAY
    if day.weekday() not in schedule.hours:
        return CLOSED
    return None


def calendar_rule(schedule: Schedule, start: datetime, duration_min: int, now: datetime) -> str | None:
    """Rules 1–4: everything except other bookings."""
    tz = ZoneInfo(schedule.timezone)
    local_start = _local(start, tz)
    day = local_start.date()
    if reason := day_rule(schedule, day):
        return reason
    if local_start.minute % 15 or local_start.second or local_start.microsecond:
        return NOT_ON_STEP
    opens, closes = schedule.hours[day.weekday()]
    if local_start.time() < opens or local_start.time() >= closes:
        return OUTSIDE_HOURS
    local_end = local_start + timedelta(minutes=duration_min)
    # Past midnight counts as overflowing too (a 23:30 close with a 60-minute service).
    if local_end.date() != day or local_end.time() > closes:
        return OVERFLOWS_CLOSING
    if start < now + LEAD:
        return TOO_SOON
    return None


def check_slot(
    schedule: Schedule, start: datetime, duration_min: int, now: datetime,
    practitioner_busy: Sequence[Interval], customer_busy: Sequence[Interval],
) -> str | None:  # fmt: skip
    """None if `start` is bookable for this practitioner and customer, else the first reason."""
    if reason := calendar_rule(schedule, start, duration_min, now):
        return reason
    end = start + timedelta(minutes=duration_min)
    if any(b.overlaps(start, end) for b in practitioner_busy):
        return PRACTITIONER_BUSY
    if any(b.overlaps(start, end) for b in customer_busy):
        return CUSTOMER_BUSY
    return None


def candidate_starts(schedule: Schedule, day: date, duration_min: int) -> list[datetime]:
    """Every start on `day` that passes the calendar rules apart from "now": open day, not
    a holiday, quarter-hour steps from opening, full duration before closing. UTC."""
    if day_rule(schedule, day):
        return []
    tz = ZoneInfo(schedule.timezone)
    opens, closes = schedule.hours[day.weekday()]
    # Round opening up to the next quarter hour (hours are normally on the hour anyway).
    first_minute = -(-(opens.hour * 60 + opens.minute) // 15) * 15
    cursor = datetime.combine(day, time(0), tzinfo=tz) + timedelta(minutes=first_minute)
    duration = timedelta(minutes=duration_min)
    close_at = datetime.combine(day, closes, tzinfo=tz)
    starts = []
    while cursor + duration <= close_at:
        starts.append(cursor.astimezone(ZoneInfo("UTC")))
        cursor += STEP
    return starts


def free_slots(
    schedule: Schedule, duration_min: int, days: Iterable[date], practitioners: Mapping[uuid.UUID, str],
    busy: Mapping[uuid.UUID, Sequence[Interval]], customer_busy: Sequence[Interval], now: datetime,
    window: tuple[time, time] | None = None,
) -> list[Slot]:  # fmt: skip
    """Every bookable (start, practitioner) on `days`, ordered by start then practitioner name.
    `window` (org-local, [from, to)) keeps only starts inside a part of the day."""
    tz = ZoneInfo(schedule.timezone)
    duration = timedelta(minutes=duration_min)
    out: list[Slot] = []
    by_name = sorted(practitioners.items(), key=lambda kv: (kv[1], str(kv[0])))
    for day in sorted(set(days)):
        for start in candidate_starts(schedule, day, duration_min):
            if start < now + LEAD:
                continue
            if window and not (window[0] <= _local(start, tz).time() < window[1]):
                continue
            end = start + duration
            if any(c.overlaps(start, end) for c in customer_busy):
                continue
            for pid, name in by_name:
                if not any(b.overlaps(start, end) for b in busy.get(pid, ())):
                    out.append(Slot(start, end, pid, name))
    return out


def nearest(target: datetime, slots: Sequence[Slot], preferred: uuid.UUID | None = None, k: int = 3) -> list[Slot]:
    """Up to k free slots closest to `target`, one per start time.

    With a preferred practitioner, their slots come first (closest first); any other
    qualified practitioner fills the remaining places. Ties go to the earlier slot, then
    to the practitioner whose name sorts first, so the result is deterministic.
    """

    def key(s: Slot) -> tuple[timedelta, datetime, str]:
        return abs(s.start - target), s.start, s.practitioner_name

    picked: list[Slot] = []
    seen: set[datetime] = set()
    groups = (
        [sorted((s for s in slots if s.practitioner_id == preferred), key=key), sorted(slots, key=key)]
        if preferred is not None
        else [sorted(slots, key=key)]
    )
    for group in groups:
        for s in group:
            if len(picked) == k:
                return picked
            if s.start not in seen:
                picked.append(s)
                seen.add(s.start)
    return picked
