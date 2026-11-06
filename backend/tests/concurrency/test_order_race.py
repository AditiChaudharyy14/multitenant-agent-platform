"""Two customers confirm a proposal for the last unit at the same moment: exactly one order.

Both proposals are valid when made (stock 1). The commits run concurrently on separate
app_user connections; the product row lock plus the atomic decrement let one win, and
the other is told the item is no longer available. Stock never goes negative.
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.tools.common.confirm_proposal import confirm_proposal
from app.agents.tools.store import create_order
from tests.factories import Shop
from tests.store_support import call, proposal_id, scalar, stock, turn_ctx

pytestmark = pytest.mark.usefixtures("as_app_user")


@pytest.mark.parametrize("attempt", range(5))  # a race test is only convincing when repeated
async def test_last_unit_sold_once(shop: Shop, owner_engine: AsyncEngine, attempt: int) -> None:
    pids = {}
    for user in (shop.x, shop.y):
        result = await call(create_order, turn_ctx(shop, user), items=[{"sku": "BLK-1", "qty": 1}],
                            delivery_address="Somewhere 1")  # fmt: skip
        pids[user] = str(proposal_id(result))

    outcomes = await asyncio.gather(
        *(call(confirm_proposal, turn_ctx(shop, user), proposal_id=pids[user]) for user in (shop.x, shop.y))
    )

    placed = [o for o in outcomes if any(c["type"] == "order_confirmed" for c in o.cards)]
    assert len(placed) == 1, [o.content_for_model for o in outcomes]
    assert await scalar(owner_engine, shop, "SELECT count(*) FROM orders WHERE org_id = :o") == 1
    assert await stock(owner_engine, shop, "BLK-1") == 0
    assert await scalar(owner_engine, shop, "SELECT min(stock) FROM products WHERE org_id = :o") >= 0
