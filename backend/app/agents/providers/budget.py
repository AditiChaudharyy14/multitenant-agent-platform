"""Per-model daily request budget, shared by every process through Postgres.

Gemini's free tier caps requests per model per day and resets at midnight Pacific time.
Counting here (in llm_usage) lets the app fail fast with a clear message before the vendor
starts returning 429s, and the backend and the eval runner share one count.

Each reserve is its own tiny transaction on purpose: a request sent to the vendor is spent
even if the chat request that sent it later rolls back. llm_usage is global (no org_id):
the quota belongs to the API key, not to a tenant.
"""

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock

log = logging.getLogger("app.llm")

QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # where Google's daily quota resets
WARN_AT = 0.9  # warn once 90% of a model's daily budget is used

# Atomic check-and-increment: the WHERE makes ON CONFLICT skip the update (and return no
# row) once the limit is reached, even with many concurrent callers.
_RESERVE = text(
    "INSERT INTO llm_usage AS u (provider, model, day, requests) VALUES (:p, :m, :d, 1) "
    "ON CONFLICT (provider, model, day) DO UPDATE SET requests = u.requests + 1, updated_at = now() "
    "WHERE u.requests < :limit RETURNING requests"
)
_EXHAUST = text(
    "INSERT INTO llm_usage AS u (provider, model, day, requests) VALUES (:p, :m, :d, :limit) "
    "ON CONFLICT (provider, model, day) DO UPDATE "
    "SET requests = GREATEST(u.requests, :limit), updated_at = now()"
)
_USED = text("SELECT requests FROM llm_usage WHERE provider = :p AND model = :m AND day = :d")


def quota_day(now: datetime | None = None) -> date:
    return (now or clock.now()).astimezone(QUOTA_TZ).date()


def next_reset(now: datetime | None = None) -> datetime:
    """The next midnight in QUOTA_TZ, as an aware datetime."""
    tomorrow = quota_day(now) + timedelta(days=1)
    return datetime.combine(tomorrow, time(0), tzinfo=QUOTA_TZ).astimezone(ZoneInfo("UTC"))


class DailyBudget:
    def __init__(self, provider: str, limit: int, engine: AsyncEngine | None = None) -> None:
        self.provider = provider
        self.limit = limit
        self._engine = engine

    @property
    def engine(self) -> AsyncEngine:
        # Imported lazily so building a provider never opens a connection at import time.
        if self._engine is None:
            from app.db.session import app_engine

            self._engine = app_engine
        return self._engine

    def _params(self, model: str) -> dict:
        return {"p": self.provider, "m": model, "d": quota_day(), "limit": self.limit}

    async def reserve(self, model: str) -> int | None:
        """Count one request against today's budget. Returns the new count, or None at the limit."""
        if self.limit <= 0:
            return None
        async with self.engine.begin() as conn:
            count = await conn.scalar(_RESERVE, self._params(model))
        if count is None:
            return None
        left = self.limit - count
        msg = f"{self.provider}/{model}: {count}/{self.limit} requests today, {left} left"
        log.log(logging.WARNING if count >= WARN_AT * self.limit else logging.INFO, msg)
        return count

    async def exhaust(self, model: str) -> None:
        """The vendor says today's quota is gone: stop sending to this model until the reset."""
        async with self.engine.begin() as conn:
            await conn.execute(_EXHAUST, self._params(model))
        log.warning(f"{self.provider}/{model}: vendor reports daily quota exhausted")

    async def used(self, model: str) -> int:
        async with self.engine.connect() as conn:
            return await conn.scalar(_USED, self._params(model)) or 0
