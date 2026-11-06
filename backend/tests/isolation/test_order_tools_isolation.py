"""Every order tool, given someone else's order number, says "not found" and changes nothing.

Customer X tries customer Y's order (same org: RLS own_rows hides it) and an order from
another org (tenant_isolation hides it). Both replies must be identical to the reply for a
number that doesn't exist at all, so a guess can't tell "exists, not yours" from "absent".
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.tools.store import cancel_order, get_order_status, reorder, update_order
from tests.factories import Shop, insert_shop_order
from tests.store_support import call, db_snapshot, turn_ctx

pytestmark = pytest.mark.usefixtures("as_app_user")

CASES = {
    "get_order_status": (get_order_status, {}),
    "update_order": (update_order, {"delivery_address": "Somewhere else"}),
    "cancel_order": (cancel_order, {}),
    "reorder": (reorder, {}),
}


@pytest.mark.parametrize("name", CASES)
async def test_foreign_order_numbers_are_not_found(
    name: str, shop: Shop, other_shop: Shop, owner_engine: AsyncEngine
) -> None:
    tool, extra = CASES[name]
    y_number = await insert_shop_order(owner_engine, shop, shop.y, [("GRN-1", 1)])
    other_number = await insert_shop_order(owner_engine, other_shop, other_shop.x, [("GRN-1", 1)])
    # X needs an order of their own, so "not found" can't be explained by having none.
    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)])
    before = (await db_snapshot(owner_engine, shop), await db_snapshot(owner_engine, other_shop))

    ctx = turn_ctx(shop, shop.x)
    replies = [
        await call(tool, ctx, order_number=shop.number(y_number), **extra),  # same org, other customer
        await call(tool, ctx, order_number=other_shop.number(other_number), **extra),  # other org's prefix
        await call(tool, ctx, order_number=shop.number(other_number), **extra),  # other org's number, our prefix
        await call(tool, ctx, order_number=shop.number(99999), **extra),  # doesn't exist
    ]

    texts = {r.content_for_model for r in replies}
    assert len(texts) == 1, texts
    [text] = texts
    assert "NOT FOUND" in text
    assert all(r.cards == [] for r in replies)
    assert (await db_snapshot(owner_engine, shop), await db_snapshot(owner_engine, other_shop)) == before


async def test_list_my_orders_never_shows_others(shop: Shop, other_shop: Shop, owner_engine: AsyncEngine) -> None:
    from app.agents.tools.store import list_my_orders

    await insert_shop_order(owner_engine, shop, shop.y, [("GRN-1", 1)])
    await insert_shop_order(owner_engine, other_shop, other_shop.x, [("GRN-1", 1)])
    result = await call(list_my_orders, turn_ctx(shop, shop.x))
    assert [c["orders"] for c in result.cards if c["type"] == "order_list"] in ([[]], [])


async def test_cannot_confirm_another_customers_proposal(shop: Shop, owner_engine: AsyncEngine) -> None:
    from app.agents.tools.common.confirm_proposal import confirm_proposal
    from app.agents.tools.store import create_order
    from tests.store_support import card

    proposed = await call(create_order, turn_ctx(shop, shop.y), items=[{"sku": "GRN-1", "qty": 1}],
                          delivery_address="Y's house")  # fmt: skip
    pid = card(proposed, "order_summary")["proposal_id"]
    before = await db_snapshot(owner_engine, shop)
    # X, even in Y's conversation id, can't see or confirm Y's proposal.
    for conv in (shop.conversations[shop.x], shop.conversations[shop.y]):
        outcome = await call(confirm_proposal, turn_ctx(shop, shop.x, conversation=conv), proposal_id=pid)
        assert "NOT FOUND" in outcome.content_for_model
    assert await db_snapshot(owner_engine, shop) == before
