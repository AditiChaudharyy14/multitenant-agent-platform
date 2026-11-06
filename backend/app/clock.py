"""The single source of "now". All application code calls now() instead of datetime.now()."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

# A ContextVar rather than a global, so a freeze can't leak into concurrent tasks that
# didn't start inside it.
_frozen: ContextVar[datetime | None] = ContextVar("frozen_now", default=None)


def now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return _frozen.get() or datetime.now(UTC)


@contextmanager
def freeze(at: datetime) -> Iterator[datetime]:
    """Make now() return `at` inside this block. `at` must be timezone-aware."""
    if at.tzinfo is None:
        raise ValueError("freeze() needs a timezone-aware datetime")
    frozen = at.astimezone(UTC)
    token = _frozen.set(frozen)
    try:
        yield frozen
    finally:
        _frozen.reset(token)
