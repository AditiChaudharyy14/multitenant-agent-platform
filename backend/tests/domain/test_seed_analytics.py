"""analytics.yaml: `make seed` adds three weeks of history, idempotently, after the base rows
(whose numbers never move), only up to the seed "now", and never a future appointment.

Each test reseeds the shared app_test orgs with analytics and puts the base seed back
afterwards, so the other seed tests keep seeing data.yaml only.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock
from app.config import get_settings, require_owner_url
from app.db.session import TenantContext, readonly_tx
from app.domain.analytics.metrics import compute
from app.domain.analytics.windows import resolve_window
from app.seed.run import apply_analytics, seed_all
from app.tenancy.orgs import get_org_by_id
from tests.conftest import SEED_NOW
from tests.domain.test_seed import snapshot
from tests.factories import set_owner_tenant

TZ = ZoneInfo("Asia/Kathmandu")
ANCHOR = datetime(2026, 11, 9, 10, 0, tzinfo=TZ)  # SEED_NOW


@pytest.fixture
async def with_analytics(seeded: dict[str, uuid.UUID]) -> AsyncIterator[dict[str, uuid.UUID]]:
    await seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW)
    yield seeded
    await seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW, analytics=False)


async def _scalar(owner: AsyncEngine, org: uuid.UUID, sql: str) -> object:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, org)
        return await conn.scalar(text(sql))


async def _metric(org_id: uuid.UUID, name: str, window: str) -> dict:
    async with readonly_tx(TenantContext(org_id, None, "admin")) as conn:
        org = await get_org_by_id(conn, org_id)
        assert org is not None
        return (await compute(conn, org, name, resolve_window(window, org.timezone, ANCHOR)))["values"]


async def test_analytics_seed_is_idempotent(with_analytics: dict[str, uuid.UUID], owner_engine: AsyncEngine) -> None:
    before = {v: await snapshot(owner_engine, o) for v, o in with_analytics.items()}
    await seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW)
    assert {v: await snapshot(owner_engine, o) for v, o in with_analytics.items()} == before
    assert before["store"]["counts"]["orders"] > 20 and before["store"]["counts"]["stock_events"] == 5
    assert before["booking"]["counts"]["bookings"] > 20


async def test_base_rows_keep_their_numbers(seeded: dict[str, uuid.UUID], owner_engine: AsyncEngine) -> None:
    """History comes after the base rows, so README order/booking numbers don't change."""
    base = await snapshot(owner_engine, seeded["store"])
    base_numbers = await _scalar(owner_engine, seeded["store"], "SELECT array_agg(order_number ORDER BY id) FROM orders")
    await seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW)
    try:
        ids = [o[0] for o in base["orders"]]
        async with owner_engine.begin() as conn:
            await set_owner_tenant(conn, seeded["store"])
            numbers = list(await conn.scalars(text("SELECT order_number FROM orders WHERE id = ANY(:i) ORDER BY id"), {"i": ids}))
        assert numbers == base_numbers
    finally:
        await seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW, analytics=False)


async def test_history_bookings_are_past_and_fit_the_schedule(with_analytics: dict[str, uuid.UUID], owner_engine: AsyncEngine) -> None:
    org = with_analytics["booking"]
    async with owner_engine.begin() as conn:
        await set_owner_tenant(conn, org)
        hours = {wd: (o, c) for wd, o, c in (await conn.execute(text("SELECT weekday, opens, closes FROM opening_hours"))).all()}
        holidays = set(await conn.scalars(text("SELECT day FROM holidays")))
        rows = (await conn.execute(text(
            "SELECT b.status, b.starts_at, b.ends_at, b.price, b.cancellation_fee, b.conversation_id, c.created_at "
            "FROM bookings b LEFT JOIN conversations c ON c.id = b.conversation_id WHERE b.conversation_id IS NOT NULL"))).all()  # fmt: skip
    assert len(rows) >= 18
    for status, start, end, price, fee, conv, chat_started in rows:
        local_start, local_end = start.astimezone(TZ), end.astimezone(TZ)
        assert status in ("completed", "cancelled") and end <= ANCHOR
        assert local_start.date() not in holidays
        opens, closes = hours[local_start.weekday()]
        assert opens <= local_start.time() and local_end.time() <= closes and local_end.date() == local_start.date()
        assert chat_started < start
        assert (fee is None) == (status != "cancelled")
        assert fee in (None, 0, price / 2)
    assert any(r.cancellation_fee for r in rows), "some cancellation has a fee"


async def test_nothing_is_dated_after_the_seed_now(with_analytics: dict[str, uuid.UUID], owner_engine: AsyncEngine) -> None:
    for org in with_analytics.values():
        for table, col in (("users", "created_at"), ("conversations", "created_at"), ("messages", "created_at"),
                           ("agent_runs", "created_at"), ("orders", "updated_at"), ("stock_events", "created_at")):  # fmt: skip
            latest = await _scalar(owner_engine, org, f"SELECT max({col}) FROM {table}")  # noqa: S608
            assert latest is None or latest <= ANCHOR, table


async def test_an_early_seed_now_leaves_out_later_rows(seeded: dict[str, uuid.UUID], owner_engine: AsyncEngine) -> None:
    """Seeded at 07:00, today's 07:30 sign-up and the 08:10 / 09:05 chats don't exist yet."""
    await seed_all(require_owner_url(), get_settings().seed_dir, "2026-11-09T07:00")
    try:
        org = seeded["store"]
        assert await _scalar(owner_engine, org, "SELECT count(*) FROM users WHERE email = 'asmita@himali.test'") == 0
        n = await _scalar(owner_engine, org, "SELECT count(*) FROM conversations WHERE created_at >= '2026-11-09T07:00+05:45'")
        assert n == 0
    finally:
        await seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW, analytics=False)


async def test_metrics_are_not_zero_on_the_seeded_history(with_analytics: dict[str, uuid.UUID]) -> None:
    store, booking = with_analytics["store"], with_analytics["booking"]
    assert (await _metric(store, "orders_by_status", "today"))["total"] >= 3
    assert (await _metric(store, "revenue", "this_week"))["orders"] > 3
    assert (await _metric(store, "most_out_of_stock", "this_month"))["products"][0]["sku"] == "HT-BLK-004"
    assert (await _metric(store, "conversion_rate", "this_month"))["conversion_rate_pct"] not in (None, "0.0")
    assert (await _metric(store, "new_signups", "last_7_days"))["new_signups"] == 3  # a7, a8, a9
    assert (await _metric(booking, "cancellations", "this_month"))["cancellations"] >= 3
    util = await _metric(booking, "practitioner_utilisation", "this_month")
    assert all(p["booked_minutes"] > 0 for p in util["practitioners"])
    assert (await _metric(booking, "ai_usage", "this_month"))["runs"] > 20


async def test_apply_analytics_adds_the_same_history_to_a_base_seeded_org(seeded: dict[str, uuid.UUID], owner_engine: AsyncEngine) -> None:
    """What the admin eval scenarios do to their clone: same rows as `make seed` would add."""
    await seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW)
    full = {v: await snapshot(owner_engine, o) for v, o in seeded.items()}
    await seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW, analytics=False)
    try:
        for org_id in seeded.values():
            async with owner_engine.begin() as conn:
                await set_owner_tenant(conn, org_id)
                slug = await conn.scalar(text("SELECT slug FROM orgs WHERE id = :o"), {"o": org_id})
                with clock.freeze(ANCHOR):
                    assert await apply_analytics(conn, get_settings().seed_dir, slug, org_id) > 0
        after = {v: await snapshot(owner_engine, o) for v, o in seeded.items()}
        assert after == full
    finally:
        await seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW, analytics=False)
