"""The single source of "now". All application code calls now() instead of datetime.now().

Two readings of the same clock:
  now()       the business "now": dates, slots, proposals, sessions, prompts. Evals and
              tests freeze it.
  wall_now()  the real-world time, for things the outside world counts: the LLM vendor's
              quota day. An eval run freezes the business clock at a scenario date but its
              live requests are still spent today, so it freezes with wall=False.
Both default to the real clock; freeze() freezes both unless told otherwise.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

# ContextVars rather than globals, so a freeze can't leak into concurrent tasks that
# didn't start inside it.
_frozen: ContextVar[datetime | None] = ContextVar("frozen_now", default=None)
_frozen_wall: ContextVar[datetime | None] = ContextVar("frozen_wall", default=None)


def now() -> datetime:
    """Return the current (business) time as a timezone-aware UTC datetime."""
    return _frozen.get() or datetime.now(UTC)


def wall_now() -> datetime:
    """Return the real-world time (frozen only by freeze(..., wall=True))."""
    return _frozen_wall.get() or datetime.now(UTC)


@contextmanager
def freeze(at: datetime, *, wall: bool = True) -> Iterator[datetime]:
    """Make now() (and, with wall=True, wall_now()) return `at` inside this block.
    `at` must be timezone-aware."""
    if at.tzinfo is None:
        raise ValueError("freeze() needs a timezone-aware datetime")
    frozen = at.astimezone(UTC)
    token = _frozen.set(frozen)
    wall_token = _frozen_wall.set(frozen) if wall else None
    try:
        yield frozen
    finally:
        if wall_token is not None:
            _frozen_wall.reset(wall_token)
        _frozen.reset(token)
