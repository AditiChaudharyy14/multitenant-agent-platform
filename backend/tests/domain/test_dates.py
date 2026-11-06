"""Relative dates (app/domain/slots/dates.py), resolved against a frozen "now" in the org's
timezone. Two reference points: Thu 5 Nov 2026 14:00 NPT and Mon 9 Nov 2026 10:00 NPT.

The rule for "next <weekday>": the first such weekday strictly after today. If that is
tomorrow, it is ambiguous (people often mean the week after), so the resolver returns
both candidate dates and the agent asks.
"""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.domain.slots.dates import AFTERNOON, EVENING, MORNING, parse_local_start, resolve_when

TZ_NAME = "Asia/Kathmandu"
TZ = ZoneInfo(TZ_NAME)
THURSDAY = datetime(2026, 11, 5, 14, 0, tzinfo=TZ)
MONDAY = datetime(2026, 11, 9, 10, 0, tzinfo=TZ)
SUNDAY = datetime(2026, 11, 8, 10, 0, tzinfo=TZ)

D = date  # brevity in the table

CASES = [
    # (now, text, expected days, expected window)
    (THURSDAY, "next Tuesday afternoon", [D(2026, 11, 10)], AFTERNOON),
    (THURSDAY, "tomorrow morning", [D(2026, 11, 6)], MORNING),
    (THURSDAY, "this weekend", [D(2026, 11, 7), D(2026, 11, 8)], None),
    (THURSDAY, "15 Nov", [D(2026, 11, 15)], None),
    (THURSDAY, "Nov 15", [D(2026, 11, 15)], None),
    (THURSDAY, "15th November evening", [D(2026, 11, 15)], EVENING),
    (THURSDAY, "2026-11-20", [D(2026, 11, 20)], None),
    (THURSDAY, "20 Nov 2026", [D(2026, 11, 20)], None),
    (THURSDAY, "today", [D(2026, 11, 5)], None),
    (THURSDAY, "this afternoon", [D(2026, 11, 5)], AFTERNOON),
    (THURSDAY, "Thursday", [D(2026, 11, 5)], None),  # a bare weekday includes today
    (THURSDAY, "this Monday", [D(2026, 11, 9)], None),
    (THURSDAY, "next Wednesday", [D(2026, 11, 11)], None),
    (THURSDAY, "on Tue", [D(2026, 11, 10)], None),
    (THURSDAY, "1 Nov", [D(2027, 11, 1)], None),  # no year and already past: next year's
    (MONDAY, "Tuesday", [D(2026, 11, 10)], None),
    (MONDAY, "next Thursday", [D(2026, 11, 12)], None),
    (MONDAY, "this weekend", [D(2026, 11, 14), D(2026, 11, 15)], None),
    (MONDAY, "tomorrow evening", [D(2026, 11, 10)], EVENING),
    (SUNDAY, "this weekend", [D(2026, 11, 8)], None),  # only what is left of it
    # Forms the live model actually sent: our own display format, a weekday + date, a datetime.
    (THURSDAY, "Tue 10 Nov 2026", [D(2026, 11, 10)], None),
    (THURSDAY, "Tuesday 10 November afternoon", [D(2026, 11, 10)], AFTERNOON),
    (THURSDAY, "Tuesday, November 10", [D(2026, 11, 10)], None),
    (THURSDAY, "2026-11-10 13:30", [D(2026, 11, 10)], None),
    (THURSDAY, "2026-11-10T13:30", [D(2026, 11, 10)], None),
]


@pytest.mark.parametrize(("now", "text", "days", "window"), CASES, ids=[f"{c[0]:%a}:{c[1]}" for c in CASES])
def test_resolves(now: datetime, text: str, days: list[date], window: tuple[time, time] | None) -> None:
    r = resolve_when(text, now, TZ_NAME)
    assert r.error is None, r.error
    assert not r.ambiguous
    assert r.days == days
    assert r.window == window


def test_parts_of_day() -> None:
    assert MORNING == (time(9), time(12))
    assert AFTERNOON == (time(12), time(17))
    assert EVENING[0] == time(17)  # until closing


def test_next_weekday_that_is_tomorrow_is_ambiguous() -> None:
    r = resolve_when("next Friday", THURSDAY, TZ_NAME)
    assert r.ambiguous
    assert r.candidates == [D(2026, 11, 6), D(2026, 11, 13)]
    assert r.days == []  # nothing is searched until the customer picks


def test_next_tuesday_on_monday_is_ambiguous_too() -> None:
    r = resolve_when("next Tuesday afternoon", MONDAY, TZ_NAME)
    assert r.ambiguous and r.candidates == [D(2026, 11, 10), D(2026, 11, 17)]
    assert r.window == AFTERNOON


@pytest.mark.parametrize("text", ["2026-11-01", "1 Nov 2026", "yesterday", "2025-12-31"])
def test_past_dates_are_rejected(text: str) -> None:
    r = resolve_when(text, THURSDAY, TZ_NAME)
    assert r.error is not None and "past" in r.error
    assert r.days == []


@pytest.mark.parametrize("text", ["whenever", "the 31st of Smarch", "2026-02-30", "", "Wednesday 10 November"])
def test_unparseable_is_an_error_not_a_guess(text: str) -> None:
    r = resolve_when(text, THURSDAY, TZ_NAME)
    assert r.error is not None and r.days == []


def test_resolution_uses_the_org_timezone_not_utc() -> None:
    # 23:00 NPT on Thursday is still 17:15 UTC Thursday, but 00:30 NPT Friday is Thursday in UTC.
    just_after_midnight = datetime(2026, 11, 6, 0, 30, tzinfo=TZ)
    assert just_after_midnight.astimezone(UTC).date() == D(2026, 11, 5)
    assert resolve_when("today", just_after_midnight, TZ_NAME).days == [D(2026, 11, 6)]


def test_nine_npt_is_three_fifteen_utc() -> None:
    assert parse_local_start("2026-11-10T09:00", TZ_NAME) == datetime(2026, 11, 10, 3, 15, tzinfo=UTC)


def test_start_with_an_offset_is_converted() -> None:
    assert parse_local_start("2026-11-10T03:15:00Z", TZ_NAME) == datetime(2026, 11, 10, 3, 15, tzinfo=UTC)
    assert parse_local_start("2026-11-10 14:00", TZ_NAME) == datetime(2026, 11, 10, 8, 15, tzinfo=UTC)


@pytest.mark.parametrize("raw", ["next Tuesday", "14:00", "2026-11-10", "2026-13-01T10:00"])
def test_start_must_be_an_absolute_datetime(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_local_start(raw, TZ_NAME)
