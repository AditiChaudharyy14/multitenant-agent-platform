"""stock_events: written whenever a customer asks for more than is in stock (create_order,
update_order, reorder), never otherwise; customer-owned under RLS."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.tools.store import create_order, reorder, update_order
from app.db.session import TenantContext, tenant_tx
from tests.factories import Shop, insert_shop_order
from tests.store_support import call, rows, turn_ctx

pytestmark = pytest.mark.usefixtures("as_app_user")
ADDRESS = "House 1, Thamel, Kathmandu"


async def events(owner: AsyncEngine, shop: Shop) -> list[tuple]:
    return [tuple(r) for r in await rows(
        owner, shop, "SELECT user_id, sku, requested_qty, available_qty, conversation_id FROM stock_events "
                     "WHERE org_id = :o ORDER BY created_at, sku")]  # fmt: skip


async def test_create_order_shortage_writes_one_event_per_short_line(shop: Shop, owner_engine: AsyncEngine) -> None:
    ctx = turn_ctx(shop, shop.x)
    await call(create_order, ctx, items=[{"sku": "BLK-1", "qty": 3}, {"sku": "BLK-2", "qty": 1}, {"sku": "GRN-1", "qty": 1}],
               delivery_address=ADDRESS)  # fmt: skip
    assert sorted(await events(owner_engine, shop)) == sorted([
        (shop.x, "BLK-1", 3, 1, ctx.conversation_id), (shop.x, "BLK-2", 1, 0, ctx.conversation_id)])  # fmt: skip


async def test_no_event_when_stock_suffices(shop: Shop, owner_engine: AsyncEngine) -> None:
    await call(create_order, turn_ctx(shop, shop.x), items=[{"sku": "GRN-1", "qty": 2}], delivery_address=ADDRESS)
    assert await events(owner_engine, shop) == []


async def test_update_order_shortage_counts_units_already_held(shop: Shop, owner_engine: AsyncEngine) -> None:
    number = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-3", 1)])  # stock 3, so 4 are available to it
    await call(update_order, turn_ctx(shop, shop.x), order_number=shop.number(number), items=[{"sku": "GRN-3", "qty": 6}])
    assert [(e[1], e[2], e[3]) for e in await events(owner_engine, shop)] == [("GRN-3", 6, 4)]


async def test_reorder_shortage_writes_events(shop: Shop, owner_engine: AsyncEngine) -> None:
    # BLK-1 has 1 left (reorder trims to 1), BLK-2 is sold out (left out): both are shortages.
    number = await insert_shop_order(owner_engine, shop, shop.x, [("BLK-1", 2), ("BLK-2", 1), ("GRN-1", 1)], status="delivered")
    await call(reorder, turn_ctx(shop, shop.x), order_number=shop.number(number))
    assert sorted((e[1], e[2], e[3]) for e in await events(owner_engine, shop)) == [("BLK-1", 2, 1), ("BLK-2", 1, 0)]


async def test_customer_sees_only_own_stock_events(shop: Shop) -> None:
    await call(create_order, turn_ctx(shop, shop.x), items=[{"sku": "BLK-2", "qty": 1}], delivery_address=ADDRESS)
    await call(create_order, turn_ctx(shop, shop.y), items=[{"sku": "BLK-2", "qty": 2}], delivery_address=ADDRESS)
    async with tenant_tx(TenantContext(shop.org_id, shop.y, "customer")) as conn:
        seen = (await conn.execute(text("SELECT user_id, requested_qty FROM stock_events"))).all()
    assert [tuple(r) for r in seen] == [(shop.y, 2)]
    async with tenant_tx(TenantContext(shop.org_id, shop.admin, "admin")) as conn:
        assert await conn.scalar(text("SELECT count(*) FROM stock_events")) == 2
