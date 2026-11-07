"""Time windows for admin metrics, resolved on the server in the org's timezone.

The model never works out dates: it passes a window name ("today", "this_week") or, for
explicit dates, a custom range, and this module turns that into [start, end) at local
midnights, stored as UTC. A 23:50 local order belongs to that local day, and a 00:10
local order to the next one, although in UTC (18:25 the evening before) it falls on the
earlier date.

  today        local midnight today → tomorrow's local midnight
  yesterday    the local day before today
  this_week    Monday 00:00 of this week → the next Monday 00:00
  last_7_days  today and the 6 days before it
  this_month   the 1st of this month → the 1st of next month
  custom       start_date..end_date, both inclusive local dates (at most 366 days)
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

WindowName = Literal["today", "yesterday", "this_week", "last_7_days", "this_month", "custom"]
MAX_CUSTOM_DAYS = 366


class WindowError(ValueError):
    """A window the model asked for that can't be resolved (told to the model as data)."""


@dataclass(frozen=True, slots=True)
class Window:
    name: str
    start: datetime  # UTC, inclusive
    end: datetime  # UTC, exclusive
    timezone: str
    first_day: date  # local, inclusive
    last_day: date  # local, inclusive

    @property
    def label(self) -> str:
        """'today, Thu 12 Nov 2026' or 'this week, Mon 9 Nov – Sun 15 Nov 2026'."""
        words = self.name.replace("_", " ")
        if self.first_day == self.last_day:
            return f"{words}, {_day(self.first_day)} {self.first_day.year}"
        same_year = self.first_day.year == self.last_day.year
        first = _day(self.first_day) + ("" if same_year else f" {self.first_day.year}")
        return f"{words}, {first} – {_day(self.last_day)} {self.last_day.year}"

    def as_dict(self) -> dict[str, str]:
        """What every metric returns: ISO local times with the offset, plus the timezone."""
        tz = ZoneInfo(self.timezone)
        return {
            "name": self.name,
            "label": self.label,
            "start": self.start.astimezone(tz).isoformat(),
            "end": self.end.astimezone(tz).isoformat(),
            "timezone": self.timezone,
        }

    def days(self) -> list[date]:
        """Every local date in the window."""
        return [self.first_day + timedelta(days=i) for i in range((self.last_day - self.first_day).days + 1)]


def _day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def _midnight(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time(0), tzinfo=tz).astimezone(UTC)


def resolve_window(
    name: str, timezone: str, now: datetime, start_date: date | None = None, end_date: date | None = None
) -> Window:
    """[start, end) for a named window, against `now` (pass clock.now())."""
    tz = ZoneInfo(timezone)
    today = now.astimezone(tz).date()
    if name != "custom" and (start_date or end_date):
        raise WindowError("start_date and end_date are only for window 'custom'")
    if name == "today":
        first = last = today
    elif name == "yesterday":
        first = last = today - timedelta(days=1)
    elif name == "this_week":
        first = today - timedelta(days=today.weekday())  # weekday() 0 = Monday
        last = first + timedelta(days=6)
    elif name == "last_7_days":
        first, last = today - timedelta(days=6), today
    elif name == "this_month":
        first = today.replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    elif name == "custom":
        if start_date is None or end_date is None:
            raise WindowError("window 'custom' needs start_date and end_date (YYYY-MM-DD)")
        if end_date < start_date:
            raise WindowError("end_date is before start_date")
        if (end_date - start_date).days + 1 > MAX_CUSTOM_DAYS:
            raise WindowError(f"a custom window can span at most {MAX_CUSTOM_DAYS} days")
        first, last = start_date, end_date
    else:
        raise WindowError(f"unknown window {name!r}")
    return Window(name, _midnight(first, tz), _midnight(last + timedelta(days=1), tz), timezone, first, last)
