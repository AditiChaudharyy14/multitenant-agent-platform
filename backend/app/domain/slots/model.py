"""Plain value types for the slot engine. No I/O, no database: easy to property-test.

All datetimes are timezone-aware. Storage and comparisons are in UTC; the schedule's
opening hours and holidays are org-local wall-clock values, interpreted in its timezone.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

STEP = timedelta(minutes=15)  # slots start on wall-clock quarter hours
LEAD = timedelta(minutes=60)  # the earliest bookable start is now + 60 minutes
SEARCH_DAYS = 14  # nearest alternatives look at most this far around the target


@dataclass(frozen=True, slots=True)
class Interval:
    """[start, end): half-open, like the tstzrange(..., '[)') in the EXCLUDE constraints,
    so back-to-back appointments never conflict."""

    start: datetime
    end: datetime

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return start < self.end and self.start < end


@dataclass(frozen=True, slots=True)
class Schedule:
    timezone: str  # IANA name, e.g. Asia/Kathmandu
    hours: Mapping[int, tuple[time, time]]  # weekday (0 = Monday) -> (opens, closes); absent = closed
    holidays: frozenset[date] = frozenset()
    holiday_names: Mapping[date, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Slot:
    start: datetime
    end: datetime
    practitioner_id: uuid.UUID
    practitioner_name: str


@dataclass(frozen=True, slots=True)
class Service:
    id: uuid.UUID
    name: str
    duration_min: int
    price: Decimal
    practitioners: Mapping[uuid.UUID, str]  # who is qualified to give it: id -> name


@dataclass(frozen=True, slots=True)
class Ignore:
    """A booking to leave out of the busy sets: the one being rescheduled may overlap its
    own old time. The EXCLUDE constraints allow one confirmed booking per practitioner per
    instant, so (practitioner, start, end) identifies it among the busy intervals."""

    practitioner_id: uuid.UUID
    start: datetime
    end: datetime
