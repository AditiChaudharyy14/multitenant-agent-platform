"""Per-model daily request budget, shared through Postgres (llm_usage).

Gemini's free tier counts requests per model per day and resets at midnight Pacific; the
budget mirrors that so the app fails fast with a clear message instead of a 429 storm.
"""

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime

from app import clock
from app.agents.providers.budget import DailyBudget, next_reset, quota_day


def provider() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"  # isolates each test's counters


async def test_reserve_counts_up_to_the_limit() -> None:
    b = DailyBudget(provider(), limit=3)
    assert [await b.reserve("m") for _ in range(4)] == [1, 2, 3, None]
    assert await b.used("m") == 3


async def test_concurrent_reserves_never_exceed_the_limit() -> None:
    b = DailyBudget(provider(), limit=10)
    results = await asyncio.gather(*(b.reserve("m") for _ in range(20)))
    granted = [r for r in results if r is not None]
    assert sorted(granted) == list(range(1, 11))


async def test_models_are_counted_separately() -> None:
    b = DailyBudget(provider(), limit=1)
    assert await b.reserve("primary") == 1
    assert await b.reserve("primary") is None
    assert await b.reserve("fallback") == 1


async def test_exhaust_marks_model_used_up_for_today() -> None:
    b = DailyBudget(provider(), limit=5)
    await b.reserve("m")
    await b.exhaust("m")
    assert await b.used("m") == 5
    assert await b.reserve("m") is None


async def test_day_rolls_over_at_pacific_midnight() -> None:
    b = DailyBudget(provider(), limit=1)
    # 07:59 UTC on 7 Nov 2026 is 23:59 PST on 6 Nov; 08:00 UTC is midnight PST.
    with clock.freeze(datetime(2026, 11, 7, 7, 59, tzinfo=UTC)):
        assert quota_day() == date(2026, 11, 6)
        assert await b.reserve("m") == 1
        assert await b.reserve("m") is None
        assert next_reset() == datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
    with clock.freeze(datetime(2026, 11, 7, 8, 0, tzinfo=UTC)):
        assert quota_day() == date(2026, 11, 7)
        assert await b.reserve("m") == 1


async def test_zero_limit_grants_nothing() -> None:
    assert await DailyBudget(provider(), limit=0).reserve("m") is None


async def test_logs_remaining_budget_and_warns_near_the_limit(caplog) -> None:  # noqa: ANN001
    p = provider()
    b = DailyBudget(p, limit=10)
    with caplog.at_level(logging.INFO, logger="app.llm"):
        await b.reserve("m")
        for _ in range(8):
            await b.reserve("m")
    info = [r for r in caplog.records if r.levelno == logging.INFO]
    assert f"{p}/m: 1/10 requests today, 9 left" in info[0].getMessage()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("9/10" in w for w in warnings), warnings  # 90% reached
