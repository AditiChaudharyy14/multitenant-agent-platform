"""Metric windows: local-midnight boundaries in the org timezone (Asia/Kathmandu, +05:45)."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.analytics.windows import WindowError, resolve_window

TZ = "Asia/Kathmandu"
NPT = ZoneInfo(TZ)
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=NPT)  # Thursday


def local(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NPT).astimezone(UTC)


@pytest.mark.parametrize(
    ("name", "start", "end"),
    [
        ("today", local(2026, 11, 12), local(2026, 11, 13)),
        ("yesterday", local(2026, 11, 11), local(2026, 11, 12)),
        ("this_week", local(2026, 11, 9), local(2026, 11, 16)),  # Monday start
        ("last_7_days", local(2026, 11, 6), local(2026, 11, 13)),  # today and the 6 days before
        ("this_month", local(2026, 11, 1), local(2026, 12, 1)),
    ],
)
def test_named_windows(name: str, start: datetime, end: datetime) -> None:
    w = resolve_window(name, TZ, NOW)
    assert (w.start, w.end) == (start, end)


def test_today_starts_at_local_midnight_not_utc() -> None:
    w = resolve_window("today", TZ, NOW)
    assert w.start == datetime(2026, 11, 11, 18, 15, tzinfo=UTC)
    late = local(2026, 11, 11, 23, 50)  # 23:50 local yesterday: 18:05 UTC
    early = local(2026, 11, 12, 0, 10)  # 00:10 local today: still 11 Nov in UTC
    assert not (w.start <= late < w.end)
    assert w.start <= early < w.end
    assert early.date() == date(2026, 11, 11)  # the trap a UTC date filter would fall into


def test_this_week_on_monday_and_sunday() -> None:
    monday = datetime(2026, 11, 9, 0, 0, tzinfo=NPT)
    sunday = datetime(2026, 11, 15, 23, 59, tzinfo=NPT)
    assert resolve_window("this_week", TZ, monday).start == local(2026, 11, 9)
    assert resolve_window("this_week", TZ, sunday).start == local(2026, 11, 9)
    assert resolve_window("this_week", TZ, sunday + timedelta(minutes=1)).start == local(2026, 11, 16)


def test_this_month_in_december_rolls_the_year() -> None:
    w = resolve_window("this_month", TZ, datetime(2026, 12, 31, 23, 0, tzinfo=NPT))
    assert (w.start, w.end) == (local(2026, 12, 1), local(2027, 1, 1))


def test_custom_dates_are_inclusive() -> None:
    w = resolve_window("custom", TZ, NOW, date(2026, 11, 1), date(2026, 11, 3))
    assert (w.start, w.end) == (local(2026, 11, 1), local(2026, 11, 4))
    assert w.days() == [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]


@pytest.mark.parametrize(
    ("name", "start", "end"),
    [
        ("custom", None, None),
        ("custom", date(2026, 11, 3), date(2026, 11, 1)),
        ("custom", date(2025, 1, 1), date(2026, 11, 1)),  # over 366 days
        ("today", date(2026, 11, 1), None),  # dates only with custom
        ("next_quarter", None, None),
    ],
)
def test_bad_windows_are_refused(name: str, start: date | None, end: date | None) -> None:
    with pytest.raises(WindowError):
        resolve_window(name, TZ, NOW, start, end)


def test_window_dict_states_local_boundaries_and_timezone() -> None:
    d = resolve_window("this_week", TZ, NOW).as_dict()
    assert d == {
        "name": "this_week",
        "label": "this week, Mon 9 Nov – Sun 15 Nov 2026",
        "start": "2026-11-09T00:00:00+05:45",
        "end": "2026-11-16T00:00:00+05:45",
        "timezone": TZ,
    }
    assert resolve_window("today", TZ, NOW).label == "today, Thu 12 Nov 2026"
