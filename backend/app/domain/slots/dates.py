"""Relative dates, resolved deterministically on the server against clock.now() in the
org's timezone. The model passes the customer's own words ("next Tuesday afternoon");
it never does date arithmetic itself.

Supported (case-insensitive, an optional leading "on"):
  today, tonight, tomorrow, yesterday (past: rejected)
  <weekday>, this <weekday>   the next such day on or after today
  next <weekday>              the first such day strictly after today; if that is
                              tomorrow it is AMBIGUOUS (many people mean the week after),
                              so both candidates come back and the agent asks
  this weekend, weekend       the coming Saturday and Sunday (what is left of it, if today
                              is already a weekend day)
  15 Nov, 15th November, Nov 15     no year: this year, or next year once it has passed
  15 Nov 2026, 2026-11-15           explicit year: a past date is rejected
  Tue 10 Nov 2026, Tuesday 10 November   a weekday plus a date (our cards' own format);
                                         the weekday must match the date
  2026-11-10 13:30                  a datetime: only its date is used
plus an optional part of day: morning 09–12, afternoon 12–17, evening 17–close.
A part of day alone ("this afternoon") means today.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MORNING = (time(9), time(12))
AFTERNOON = (time(12), time(17))
EVENING = (time(17), time(23, 59))  # until closing: the slot engine stops at closing anyway
PARTS = {"morning": MORNING, "afternoon": AFTERNOON, "evening": EVENING, "tonight": EVENING}

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}  # fmt: skip
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip

_MONTH = r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?:,?\s+(?P<year>\d{4}))?"
_DAY_MONTH = re.compile(rf"^{_DAY}\s+(?:of\s+)?{_MONTH}{_YEAR}$")
_MONTH_DAY = re.compile(rf"^{_MONTH}\s+{_DAY}{_YEAR}$")
_ISO = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})$")
_WEEKDAY = re.compile(r"^(?:(?P<mod>this|next|coming)\s+)?(?P<wd>[a-z]+)$")

UNPARSEABLE = (
    "couldn't understand the date {text!r}. Ask the customer for a day, or pass date_from/"
    "date_to as YYYY-MM-DD."
)


@dataclass(frozen=True, slots=True)
class Resolved:
    days: list[date] = field(default_factory=list)  # org-local dates to search
    window: tuple[time, time] | None = None  # part of day, org-local [from, to)
    ambiguous: bool = False
    candidates: list[date] = field(default_factory=list)  # when ambiguous: the readings to ask about
    error: str | None = None


def _today(now: datetime, tz: str) -> date:
    return now.astimezone(ZoneInfo(tz)).date()


def _strip_part_of_day(text: str) -> tuple[str, tuple[time, time] | None]:
    window = None
    for word, span in PARTS.items():
        pattern = rf"\b(?:in the |this )?{word}\b"
        if re.search(pattern, text):
            window = span
            # "this afternoon" means today; keep "this" from swallowing a following weekday.
            text = re.sub(pattern, " ", text)
            break
    return " ".join(text.split()), window


def _month_number(name: str) -> int:
    return MONTHS[name[:4] if name.startswith("sept") else name[:3]]


def _past(d: date) -> Resolved:
    return Resolved(error=f"{d:%a} {d.day} {d:%b %Y} is in the past; bookings can only be made for today or later.")


def _dated(text: str, today: date) -> Resolved | None:
    """Explicit calendar dates; None if `text` isn't one."""
    m = _ISO.match(text) or _DAY_MONTH.match(text) or _MONTH_DAY.match(text)
    if not m:
        return None
    try:
        month = int(m["month"]) if m["month"].isdigit() else _month_number(m["month"])
        day_n = int(m["day"])
        if m["year"]:
            d = date(int(m["year"]), month, day_n)
            return _past(d) if d < today else Resolved(days=[d])
        d = date(today.year, month, day_n)
        if d < today:  # "1 Nov" said on 5 Nov means next year's
            d = date(today.year + 1, month, day_n)
        return Resolved(days=[d])
    except ValueError:  # 30 Feb and friends
        return Resolved(error=UNPARSEABLE.format(text=text))


def _weekday_dated(text: str, today: date) -> Resolved | None:
    """'Tue 10 Nov 2026', 'Tuesday, November 10': a weekday plus a date (the format our own
    cards use). The weekday must agree with the date; if not, ask rather than pick one."""
    m = re.match(r"^(?P<wd>[a-z]+),?\s+(?P<rest>.+)$", text)
    if not m or m["wd"] not in WEEKDAYS:
        return None
    found = _dated(m["rest"], today)
    if found is None or found.error:
        return found
    d = found.days[0]
    if d.weekday() != WEEKDAYS[m["wd"]]:
        return Resolved(error=f"{d.day} {d:%b %Y} is a {d:%A}, not a {m['wd'].capitalize()}. Ask the customer which they mean.")
    return found


def _relative(text: str, today: date) -> Resolved | None:
    if text in ("", "today", "tonight"):
        return Resolved(days=[today])
    if text == "tomorrow":
        return Resolved(days=[today + timedelta(days=1)])
    if text == "yesterday":
        return _past(today - timedelta(days=1))
    if text in ("weekend", "this weekend", "the weekend", "coming weekend", "this coming weekend"):
        if today.weekday() == 6:
            return Resolved(days=[today])  # Sunday: only today is left of it
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        return Resolved(days=[saturday, saturday + timedelta(days=1)])
    m = _WEEKDAY.match(text)
    if not m or m["wd"] not in WEEKDAYS:
        return None
    wd = WEEKDAYS[m["wd"]]
    ahead = (wd - today.weekday()) % 7
    if m["mod"] == "next":
        ahead = ahead or 7  # strictly after today
        first = today + timedelta(days=ahead)
        if ahead == 1:  # "next Friday" said on Thursday: tomorrow, or a week later?
            return Resolved(ambiguous=True, candidates=[first, first + timedelta(days=7)])
        return Resolved(days=[first])
    return Resolved(days=[today + timedelta(days=ahead)])  # bare / this / coming: today counts


def resolve_when(text: str, now: datetime, tz: str) -> Resolved:
    """Resolve the customer's words against `now` in the org's timezone. Never guesses:
    anything it can't read comes back as an error for the agent to ask about."""
    today = _today(now, tz)
    raw = (text or "").strip().lower()
    if not raw:
        return Resolved(error=UNPARSEABLE.format(text=text))
    cleaned = re.sub(r"[.,!?]+$", "", raw)
    cleaned, window = _strip_part_of_day(cleaned)
    cleaned = re.sub(r"^(?:on|for)\s+", "", cleaned)
    # "2026-11-10 13:30": a datetime where a day was asked for; the day is what counts.
    cleaned = re.sub(r"^(\d{4}-\d{2}-\d{2})[t ]\d{1,2}:\d{2}(?::\d{2})?$", r"\1", cleaned)
    found = _dated(cleaned, today) or _weekday_dated(cleaned, today) or _relative(cleaned, today)
    if found is None:
        return Resolved(error=UNPARSEABLE.format(text=text))
    if found.error:
        return found
    return Resolved(days=found.days, window=window, ambiguous=found.ambiguous, candidates=found.candidates)


def parse_iso_date(raw: str) -> date:
    """A YYYY-MM-DD date (for date_from/date_to). Raises ValueError."""
    if not _ISO.match(raw.strip()):
        raise ValueError(f"expected YYYY-MM-DD, got {raw!r}")
    return date.fromisoformat(raw.strip())


def parse_local_start(raw: str, tz: str) -> datetime:
    """An absolute start time from the model, as UTC.

    "2026-11-10T14:00" (no offset) is org-local wall-clock time, the form slot cards use.
    An explicit offset ("...Z", "+05:45") is honoured. Dates without a time, and relative
    phrases, raise ValueError: the model must pass a start it got from a tool.
    """
    text = (raw or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
        raise ValueError(f"start must look like 2026-11-10T14:00, got {raw!r}")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz))
    return parsed.astimezone(UTC)


def local_iso(dt: datetime, tz: str) -> str:
    """The inverse for cards and buttons: UTC → '2026-11-10T14:00' in the org's timezone."""
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")
