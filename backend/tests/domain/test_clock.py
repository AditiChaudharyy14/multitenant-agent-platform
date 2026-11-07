import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app import clock

KATHMANDU = timezone(timedelta(hours=5, minutes=45))


def test_now_is_timezone_aware_utc() -> None:
    assert clock.now().utcoffset() == timedelta(0)


def test_freeze_fixes_now_and_restores_it() -> None:
    frozen_at = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    with clock.freeze(frozen_at):
        assert clock.now() == frozen_at
    assert clock.now() != frozen_at


def test_freeze_normalises_to_utc() -> None:
    with clock.freeze(datetime(2026, 11, 5, 17, 45, tzinfo=KATHMANDU)):
        assert clock.now() == datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
        assert clock.now().utcoffset() == timedelta(0)


def test_freeze_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        with clock.freeze(datetime(2026, 11, 5, 12, 0)):
            pass


async def test_freeze_does_not_leak_into_concurrent_tasks() -> None:
    frozen_at = datetime(2000, 1, 1, tzinfo=UTC)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def frozen_task() -> None:
        with clock.freeze(frozen_at):
            entered.set()
            await release.wait()

    task = asyncio.create_task(frozen_task())
    await entered.wait()
    assert clock.now() != frozen_at  # this task never entered the freeze
    release.set()
    await task


def test_freeze_without_wall_leaves_the_vendor_quota_day_real() -> None:
    """An eval freezes the business clock at a scenario date; its live LLM requests must
    still be counted on today's quota day."""
    from app.agents.providers.budget import quota_day

    real_day = quota_day()
    with clock.freeze(datetime(2030, 1, 1, tzinfo=UTC), wall=False):
        assert clock.now() == datetime(2030, 1, 1, tzinfo=UTC)
        assert quota_day() == real_day
