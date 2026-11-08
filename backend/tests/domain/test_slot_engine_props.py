"""Property tests for the pure slot engine (app/domain/slots/engine.py).

Hypothesis generates random weekly hours, holidays, practitioner and customer busy
intervals, durations and "now". For every input:
  - each returned slot is inside opening hours, not on a holiday, fits the full duration
    before closing, sits on a 15-minute step, starts at least 60 minutes after now, and
    overlaps no busy interval of its practitioner nor of the customer;
  - the engine returns EXACTLY what a brute-force oracle accepts, so it is complete as
    well as sound (it never hides a free slot);
  - check_slot agrees with free_slots on every candidate start;
  - nearest() picks at most k free slots, the preferred practitioner's first, closest first.
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.domain.slots import engine
from app.domain.slots.model import LEAD, STEP, Interval, Schedule, Slot

TZ_NAME = "Asia/Kathmandu"
TZ = ZoneInfo(TZ_NAME)
BASE_DAY = date(2026, 11, 2)  # a Monday
DAYS = [BASE_DAY + timedelta(days=i) for i in range(7)]
P1, P2 = uuid.UUID(int=1), uuid.UUID(int=2)
PRACTITIONERS = {P1: "Asha", P2: "Bina"}

quarter = st.integers(min_value=0, max_value=95)  # quarter-hours in a day


@st.composite
def schedules(draw: st.DrawFn) -> Schedule:
    hours = {}
    for weekday in range(7):
        if draw(st.booleans()) or weekday == 0:  # at least one open day
            opens = draw(st.integers(min_value=6 * 4, max_value=12 * 4))
            closes = draw(st.integers(min_value=opens + 1, max_value=22 * 4))
            hours[weekday] = (time(opens // 4, opens % 4 * 15), time(closes // 4, closes % 4 * 15))
    holidays = frozenset(draw(st.lists(st.sampled_from(DAYS), max_size=3)))
    return Schedule(timezone=TZ_NAME, hours=hours, holidays=holidays)


@st.composite
def intervals(draw: st.DrawFn) -> Interval:
    # 5-minute granularity, so busy intervals straddle quarter-hour starts in awkward ways.
    day = draw(st.sampled_from(DAYS))
    start = datetime.combine(day, time(0), tzinfo=TZ) + timedelta(minutes=5 * draw(st.integers(0, 287)))
    length = timedelta(minutes=5 * draw(st.integers(1, 48)))
    return Interval(start.astimezone(UTC), (start + length).astimezone(UTC))


durations = st.sampled_from([15, 30, 45, 60, 75, 90, 120, 180])
nows = st.integers(min_value=-2 * 96, max_value=8 * 96).map(
    lambda q: (datetime.combine(BASE_DAY, time(0), tzinfo=TZ) + timedelta(minutes=q * 5)).astimezone(UTC)
)


def overlaps(a_start: datetime, a_end: datetime, b: Interval) -> bool:
    """Half-open [start, end): back-to-back is not an overlap (like tstzrange '[)')."""
    return a_start < b.end and b.start < a_end


def oracle(
    schedule: Schedule, duration: int, busy: dict, customer: list[Interval], now: datetime
) -> set[tuple[datetime, uuid.UUID]]:
    """Brute force: every quarter-hour of every day, checked rule by rule."""
    ok = set()
    for day in DAYS:
        for q in range(96):
            start_local = datetime.combine(day, time(q // 4, q % 4 * 15), tzinfo=TZ)
            end_local = start_local + timedelta(minutes=duration)
            if day.weekday() not in schedule.hours or day in schedule.holidays:
                continue
            opens, closes = schedule.hours[day.weekday()]
            if start_local.time() < opens or end_local.date() != day or end_local.time() > closes:
                continue
            start, end = start_local.astimezone(UTC), end_local.astimezone(UTC)
            if start < now + timedelta(minutes=60):
                continue
            if any(overlaps(start, end, c) for c in customer):
                continue
            for pid in PRACTITIONERS:
                if not any(overlaps(start, end, b) for b in busy.get(pid, [])):
                    ok.add((start, pid))
    return ok


inputs = st.tuples(
    schedules(), durations,
    st.fixed_dictionaries({P1: st.lists(intervals(), max_size=6), P2: st.lists(intervals(), max_size=6)}),
    st.lists(intervals(), max_size=4), nows,
)  # fmt: skip

SETTINGS = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(inputs)
def test_every_returned_slot_obeys_every_rule(args: tuple) -> None:
    schedule, duration, busy, customer, now = args
    slots = engine.free_slots(schedule, duration, DAYS, PRACTITIONERS, busy, customer, now)
    for s in slots:
        local_start, local_end = s.start.astimezone(TZ), s.end.astimezone(TZ)
        day = local_start.date()
        assert s.end - s.start == timedelta(minutes=duration)  # the full duration
        assert day.weekday() in schedule.hours, "open day"
        assert day not in schedule.holidays, "not a holiday"
        opens, closes = schedule.hours[day.weekday()]
        assert local_start.time() >= opens, "starts after opening"
        assert local_end.date() == day and local_end.time() <= closes, "ends by closing"
        assert local_start.minute % 15 == 0 and local_start.second == 0, "15-minute step"
        assert s.start >= now + LEAD, "at least 60 minutes ahead"
        assert not any(overlaps(s.start, s.end, b) for b in busy[s.practitioner_id]), "practitioner free"
        assert not any(overlaps(s.start, s.end, c) for c in customer), "customer free"
        assert s.practitioner_name == PRACTITIONERS[s.practitioner_id]


@SETTINGS
@given(inputs)
def test_engine_matches_brute_force_oracle(args: tuple) -> None:
    schedule, duration, busy, customer, now = args
    got = {
        (s.start, s.practitioner_id)
        for s in engine.free_slots(schedule, duration, DAYS, PRACTITIONERS, busy, customer, now)
    }
    assert got == oracle(schedule, duration, busy, customer, now)


@SETTINGS
@given(inputs, st.sampled_from(DAYS), quarter, st.sampled_from([P1, P2]))
def test_check_slot_agrees_with_free_slots(args: tuple, day: date, q: int, pid: uuid.UUID) -> None:
    schedule, duration, busy, customer, now = args
    start = datetime.combine(day, time(q // 4, q % 4 * 15), tzinfo=TZ).astimezone(UTC)
    reason = engine.check_slot(schedule, start, duration, now, busy[pid], customer)
    free = {
        (s.start, s.practitioner_id)
        for s in engine.free_slots(schedule, duration, [day], PRACTITIONERS, busy, customer, now)
    }
    assert (reason is None) == ((start, pid) in free), reason


def test_off_step_start_is_refused() -> None:
    schedule = Schedule(TZ_NAME, {0: (time(9), time(19))}, frozenset())
    start = datetime(2026, 11, 2, 10, 10, tzinfo=TZ).astimezone(UTC)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    assert engine.check_slot(schedule, start, 60, now, [], []) == engine.NOT_ON_STEP


def test_reasons_are_specific() -> None:
    schedule = Schedule(TZ_NAME, {0: (time(9), time(19))}, frozenset({date(2026, 11, 9)}))
    now = datetime(2026, 11, 1, tzinfo=UTC)

    def at(d: int, hh: int, mm: int = 0) -> datetime:
        return datetime(2026, 11, d, hh, mm, tzinfo=TZ).astimezone(UTC)

    assert engine.check_slot(schedule, at(3, 10), 60, now, [], []) == engine.CLOSED  # a Tuesday
    assert engine.check_slot(schedule, at(9, 10), 60, now, [], []) == engine.HOLIDAY
    assert engine.check_slot(schedule, at(2, 8), 60, now, [], []) == engine.OUTSIDE_HOURS
    assert engine.check_slot(schedule, at(2, 18, 30), 60, now, [], []) == engine.OVERFLOWS_CLOSING
    assert engine.check_slot(schedule, at(2, 18), 60, now, [], []) is None  # ends exactly at closing
    assert engine.check_slot(schedule, at(2, 10), 60, at(2, 9, 30), [], []) == engine.TOO_SOON
    busy = [Interval(at(2, 10, 30), at(2, 11))]
    assert engine.check_slot(schedule, at(2, 10), 60, now, busy, []) == engine.PRACTITIONER_BUSY
    assert engine.check_slot(schedule, at(2, 10), 60, now, [], busy) == engine.CUSTOMER_BUSY
    back_to_back = [Interval(at(2, 9), at(2, 10))]
    assert engine.check_slot(schedule, at(2, 10), 60, now, back_to_back, back_to_back) is None


slot_lists = st.lists(
    st.builds(
        lambda q, pid: Slot(
            start=(s := datetime(2026, 11, 2, tzinfo=UTC) + q * STEP), end=s + timedelta(hours=1),
            practitioner_id=pid, practitioner_name=PRACTITIONERS[pid],
        ),
        st.integers(0, 7 * 96), st.sampled_from([P1, P2]),
    ),
    max_size=40, unique_by=lambda s: (s.start, s.practitioner_id),
)  # fmt: skip


@settings(max_examples=300, deadline=None)
@given(slot_lists, st.integers(0, 7 * 96), st.sampled_from([P1, P2, None]))
def test_nearest_prefers_practitioner_then_closest(slots: list[Slot], q: int, preferred: uuid.UUID | None) -> None:
    target = datetime(2026, 11, 2, tzinfo=UTC) + q * STEP
    picked = engine.nearest(target, slots, preferred, k=3)
    assert len(picked) <= 3 and all(p in slots for p in picked)
    assert len({p.start for p in picked}) == len(picked), "one suggestion per start time"
    distinct_starts = len({s.start for s in slots})
    assert len(picked) == min(3, distinct_starts)

    def dist(s: Slot) -> tuple[timedelta, datetime]:
        return abs(s.start - target), s.start

    if preferred is not None:
        mine = [p for p in picked if p.practitioner_id == preferred]
        # The preferred practitioner's slots come first, and as many of them as possible.
        assert picked[: len(mine)] == mine
        assert len(mine) == min(3, len([s for s in slots if s.practitioner_id == preferred]))
        assert [dist(p) for p in mine] == sorted(dist(p) for p in mine)
        unpicked_mine = [s for s in slots if s.practitioner_id == preferred and s not in picked]
        if mine and unpicked_mine:
            assert max(dist(p) for p in mine) <= min(dist(s) for s in unpicked_mine)
    else:
        assert [dist(p) for p in picked] == sorted(dist(p) for p in picked)
        picked_starts = {p.start for p in picked}
        rest = [s for s in slots if s.start not in picked_starts]
        if picked and rest:
            assert max(dist(p) for p in picked) <= min(dist(s) for s in rest)
