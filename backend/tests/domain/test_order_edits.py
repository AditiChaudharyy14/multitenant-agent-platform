"""update_order, cancel_order, reorder, get_order_status and list_my_orders.

Edits and cancels only propose; the commit takes SELECT ... FOR UPDATE on the order and
re-checks 'pending' inside its transaction. Stock moves by the deltas.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.tools.common.confirm_proposal import confirm_proposal
from app.agents.tools.store import cancel_order, get_order_status, list_my_orders, reorder, update_order
from tests.factories import Shop, insert_shop_order
from tests.store_support import (
    call, card, db_snapshot, history, order_by_number, order_items, proposal_id, set_price,
    set_stock, stock, turn_ctx,
)  # fmt: skip

pytestmark = pytest.mark.usefixtures("as_app_user")


async def confirm_next_turn(shop: Shop, result):
    return await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=str(proposal_id(result)))


async def pending_order(owner: AsyncEngine, shop: Shop, items=(("GRN-1", 2), ("BLK-3", 1))) -> int:
    return await insert_shop_order(owner, shop, shop.x, list(items))


# ───────────── update_order ─────────────


async def test_update_qty_up_and_down_adjusts_stock(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await pending_order(owner_engine, shop)
    result = await call(update_order, turn_ctx(shop, shop.x), order_number=shop.number(n),
                        items=[{"sku": "GRN-1", "qty": 5}, {"sku": "BLK-3", "qty": 0}, {"sku": "GRN-2", "qty": 1}])  # fmt: skip

    summary = card(result, "order_summary")
    assert summary["kind"] == "update_order"
    assert summary["order_number"] == shop.number(n)
    diff = {d["sku"]: (d["old_qty"], d["new_qty"]) for d in summary["diff"]}
    assert diff == {"GRN-1": (2, 5), "BLK-3": (1, 0), "GRN-2": (0, 1)}
    assert summary["old_total"] == "2250.00"
    assert summary["total"] == str(Decimal("800.00") * 5 + Decimal("900.00"))
    # Proposal only.
    assert await order_items(owner_engine, shop, n) == {"GRN-1": 2, "BLK-3": 1}
    assert await stock(owner_engine, shop, "GRN-1") == 10

    done = await confirm_next_turn(shop, result)
    assert card(done, "order_updated")["order_number"] == shop.number(n)
    assert await order_items(owner_engine, shop, n) == {"GRN-1": 5, "GRN-2": 1}
    assert await stock(owner_engine, shop, "GRN-1") == 7  # 3 more taken
    assert await stock(owner_engine, shop, "BLK-3") == 21  # line removed: 1 restored
    assert await stock(owner_engine, shop, "GRN-2") == 4
    order = await order_by_number(owner_engine, shop, n)
    assert order.total == Decimal("4900.00")
    assert order.status == "pending"


async def test_update_lower_qty_restores_stock(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await pending_order(owner_engine, shop, items=(("GRN-1", 4),))
    result = await call(update_order, turn_ctx(shop, shop.x), order_number=shop.number(n), items=[{"sku": "GRN-1", "qty": 1}])
    await confirm_next_turn(shop, result)
    assert await order_items(owner_engine, shop, n) == {"GRN-1": 1}
    assert await stock(owner_engine, shop, "GRN-1") == 13


async def test_update_address(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await pending_order(owner_engine, shop)
    result = await call(update_order, turn_ctx(shop, shop.x), order_number=shop.number(n), delivery_address="Flat 3, Patan")
    summary = card(result, "order_summary")
    assert (summary["old_delivery_address"], summary["delivery_address"]) == ("House 1, Thamel, Kathmandu", "Flat 3, Patan")
    await confirm_next_turn(shop, result)
    order = await order_by_number(owner_engine, shop, n)
    assert order.delivery_address == "Flat 3, Patan"
    assert await stock(owner_engine, shop, "GRN-1") == 10


async def test_update_increase_beyond_stock_refused(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await pending_order(owner_engine, shop, items=(("GRN-2", 1),))  # GRN-2 stock 5
    before = await db_snapshot(owner_engine, shop)
    result = await call(update_order, turn_ctx(shop, shop.x), order_number=shop.number(n), items=[{"sku": "GRN-2", "qty": 7}])
    # 1 already held by the order + 5 on the shelf = 6 at most.
    [line] = card(result, "stock_issue")["lines"]
    assert (line["sku"], line["available"]) == ("GRN-2", 6)
    assert await db_snapshot(owner_engine, shop) == before


async def test_update_removing_every_item_suggests_cancel(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await pending_order(owner_engine, shop, items=(("GRN-1", 1),))
    before = await db_snapshot(owner_engine, shop)
    result = await call(update_order, turn_ctx(shop, shop.x), order_number=shop.number(n), items=[{"sku": "GRN-1", "qty": 0}])
    assert "cancel" in result.content_for_model.lower()
    assert await db_snapshot(owner_engine, shop) == before


async def test_update_needs_a_change(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await pending_order(owner_engine, shop)
    result = await call(update_order, turn_ctx(shop, shop.x), order_number=shop.number(n))
    assert not result.ok


@pytest.mark.parametrize("status", ["confirmed", "shipped", "delivered", "cancelled"])
async def test_update_non_pending_refused_with_status(shop: Shop, owner_engine: AsyncEngine, status: str) -> None:
    n = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status=status)
    before = await db_snapshot(owner_engine, shop)
    result = await call(update_order, turn_ctx(shop, shop.x), order_number=shop.number(n), delivery_address="Elsewhere")
    assert "REFUSED" in result.content_for_model and status in result.content_for_model
    assert not [c for c in result.cards if c["type"] == "order_summary"]
    assert await db_snapshot(owner_engine, shop) == before


async def test_order_shipped_between_proposal_and_confirm(shop: Shop, owner_engine: AsyncEngine) -> None:
    """The commit re-checks 'pending' under FOR UPDATE: a proposal can't edit a shipped order."""
    from app.db.session import TenantContext, tenant_tx
    from app.domain.orders.admin import advance

    n = await pending_order(owner_engine, shop)
    result = await call(update_order, turn_ctx(shop, shop.x), order_number=shop.number(n), items=[{"sku": "GRN-1", "qty": 9}])
    admin = TenantContext(org_id=shop.org_id, user_id=shop.admin, role="admin")
    for _ in range(2):  # pending → confirmed → shipped
        async with tenant_tx(admin) as conn:
            await advance(conn, n)
    before = await db_snapshot(owner_engine, shop)

    outcome = await confirm_next_turn(shop, result)

    assert "REFUSED" in outcome.content_for_model and "shipped" in outcome.content_for_model
    after = await db_snapshot(owner_engine, shop)
    assert after["orders"] == before["orders"] and after["items"] == before["items"] and after["stock"] == before["stock"]


# ───────────── cancel_order ─────────────


async def test_cancel_pending_restores_stock(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await pending_order(owner_engine, shop)
    result = await call(cancel_order, turn_ctx(shop, shop.x), order_number=shop.number(n))
    summary = card(result, "order_summary")
    assert summary["kind"] == "cancel_order"
    assert (await order_by_number(owner_engine, shop, n)).status == "pending"

    done = await confirm_next_turn(shop, result)
    assert card(done, "order_cancelled")["order_number"] == shop.number(n)
    assert (await order_by_number(owner_engine, shop, n)).status == "cancelled"
    assert await stock(owner_engine, shop, "GRN-1") == 12
    assert await stock(owner_engine, shop, "BLK-3") == 21
    assert (await history(owner_engine, shop, n))[-1] == ("pending", "cancelled", "customer")


@pytest.mark.parametrize("status", ["confirmed", "shipped", "delivered", "cancelled"])
async def test_cancel_non_pending_refused_with_status(shop: Shop, owner_engine: AsyncEngine, status: str) -> None:
    n = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status=status)
    before = await db_snapshot(owner_engine, shop)
    result = await call(cancel_order, turn_ctx(shop, shop.x), order_number=shop.number(n))
    assert "REFUSED" in result.content_for_model and status in result.content_for_model
    assert await db_snapshot(owner_engine, shop) == before


# ───────────── get_order_status / list_my_orders ─────────────


async def test_get_order_status_shows_history_in_org_time(shop: Shop, owner_engine: AsyncEngine) -> None:
    placed = datetime(2026, 11, 1, 4, 15, tzinfo=UTC)  # 10:00 in Kathmandu
    n = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 2)], status="shipped", created_at=placed)
    result = await call(get_order_status, turn_ctx(shop, shop.x), order_number=f"#{n}")
    status = card(result, "order_status")
    assert status["order_number"] == shop.number(n)
    assert status["status"] == "shipped"
    assert status["total_display"] == "NPR 1,600.00"
    assert [h["to_status"] for h in status["history"]] == ["pending", "confirmed", "shipped"]
    assert status["history"][0]["at"] == "Sun 1 Nov 2026, 10:00"
    assert status["items"] == [{"sku": "GRN-1", "name": "Green Alpha", "qty": 2, "unit_price": "800.00",
                                "line_total": "1600.00"}]  # fmt: skip
    assert "id" not in status and str(shop.org_id) not in result.content_for_model


async def test_list_my_orders_newest_first_and_own_only(shop: Shop, owner_engine: AsyncEngine) -> None:
    old = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status="delivered",
                                  created_at=datetime(2026, 10, 1, tzinfo=UTC))  # fmt: skip
    new = await insert_shop_order(owner_engine, shop, shop.x, [("BLK-3", 1)],
                                  created_at=datetime(2026, 11, 2, tzinfo=UTC))  # fmt: skip
    await insert_shop_order(owner_engine, shop, shop.y, [("GRN-1", 1)])
    result = await call(list_my_orders, turn_ctx(shop, shop.x))
    listed = card(result, "order_list")
    assert [o["order_number"] for o in listed["orders"]] == [shop.number(new), shop.number(old)]


# ───────────── reorder ─────────────


async def test_reorder_reports_price_change_and_out_of_stock(shop: Shop, owner_engine: AsyncEngine) -> None:
    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 2), ("BLK-1", 1), ("GRN-3", 1)], status="delivered",
                            created_at=datetime(2026, 10, 1, tzinfo=UTC))  # fmt: skip
    await set_price(owner_engine, shop, "GRN-1", "850.00")
    await set_stock(owner_engine, shop, "BLK-1", 0)

    result = await call(reorder, turn_ctx(shop, shop.x))

    summary = card(result, "order_summary")
    assert summary["kind"] == "reorder"
    assert {line["sku"]: line["qty"] for line in summary["lines"]} == {"GRN-1": 2, "GRN-3": 1}
    changes = summary["changes"]
    assert any("Green Alpha" in c and "800.00" in c and "850.00" in c for c in changes), changes
    assert any("Black Alpha" in c and "out of stock" in c for c in changes), changes
    # Never silently dropped: the out-of-stock line is reported, with alternatives.
    [missing] = summary["unavailable"]
    assert missing["sku"] == "BLK-1"
    assert [a["sku"] for a in missing["alternatives"]] == ["BLK-3"]
    assert summary["total"] == "3200.00"


async def test_reorder_short_item_reduced_and_reported(shop: Shop, owner_engine: AsyncEngine) -> None:
    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-3", 5)], status="delivered")  # stock 3
    result = await call(reorder, turn_ctx(shop, shop.x))
    summary = card(result, "order_summary")
    assert summary["lines"][0]["qty"] == 3
    assert any("only 3" in c for c in summary["changes"]), summary["changes"]


async def test_reorder_uses_latest_non_cancelled_order(shop: Shop, owner_engine: AsyncEngine) -> None:
    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status="delivered",
                            created_at=datetime(2026, 10, 1, tzinfo=UTC))  # fmt: skip
    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-2", 2)], status="shipped",
                            created_at=datetime(2026, 10, 15, tzinfo=UTC))  # fmt: skip
    await insert_shop_order(owner_engine, shop, shop.x, [("BLK-3", 3)], status="cancelled",
                            created_at=datetime(2026, 11, 1, tzinfo=UTC))  # fmt: skip
    result = await call(reorder, turn_ctx(shop, shop.x))
    summary = card(result, "order_summary")
    assert {line["sku"]: line["qty"] for line in summary["lines"]} == {"GRN-2": 2}
    assert summary["changes"] == []


async def test_reorder_specific_order_number_and_commit(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status="delivered",
                                address="Reorder Lane 5")  # fmt: skip
    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-2", 1)], status="delivered",
                            created_at=datetime(2026, 11, 5, tzinfo=UTC))  # fmt: skip
    result = await call(reorder, turn_ctx(shop, shop.x), order_number=shop.number(n))
    summary = card(result, "order_summary")
    assert summary["delivery_address"] == "Reorder Lane 5"
    done = await confirm_next_turn(shop, result)
    new_number = int(card(done, "order_confirmed")["order_number"].split("-")[1])
    assert await order_items(owner_engine, shop, new_number) == {"GRN-1": 1}
    assert await stock(owner_engine, shop, "GRN-1") == 9


async def test_reorder_everything_unavailable_gives_no_proposal(shop: Shop, owner_engine: AsyncEngine) -> None:
    await insert_shop_order(owner_engine, shop, shop.x, [("BLK-2", 1)], status="delivered")  # BLK-2 sold out
    result = await call(reorder, turn_ctx(shop, shop.x))
    assert not [c for c in result.cards if c["type"] == "order_summary"]
    assert "BLK-2" in result.content_for_model or "Black Beta" in result.content_for_model


async def test_reorder_with_no_orders(shop: Shop) -> None:
    result = await call(reorder, turn_ctx(shop, shop.y))
    assert not [c for c in result.cards if c["type"] == "order_summary"]
