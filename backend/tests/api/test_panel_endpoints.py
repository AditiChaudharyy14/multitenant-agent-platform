"""GET /api/orders and GET /api/bookings feed the My orders / My bookings panel.

They are built on the same domain functions as the list_my_orders / list_my_bookings
tools, so the panel can never disagree with what the agent says. Runs as app_user, so
RLS is really in force.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock
from app.agents.tools.booking import list_my_bookings
from app.agents.tools.store import list_my_orders
from tests import booking_support as bs
from tests import store_support as ss
from tests.api_support import anonymous, client_for
from tests.factories import Shop, Studio, insert_shop_order, insert_studio_booking

pytestmark = pytest.mark.usefixtures("as_app_user")


async def test_orders_endpoint_matches_the_tool(shop: Shop, owner_engine: AsyncEngine) -> None:
    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 2)])
    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status="shipped")
    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 3)], status="cancelled")
    tool = ss.card(await ss.call(list_my_orders, ss.turn_ctx(shop, shop.x)), "order_list")
    async with await client_for(shop.org_id, shop.x) as c:
        r = await c.get("/api/orders")
    assert r.status_code == 200
    assert r.json() == {"orders": tool["orders"]}
    assert len(tool["orders"]) == 3


async def test_orders_never_include_another_customers(shop: Shop, other_shop: Shop, owner_engine: AsyncEngine) -> None:
    mine = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)])
    theirs = await insert_shop_order(owner_engine, shop, shop.y, [("GRN-1", 1)])
    foreign = await insert_shop_order(owner_engine, other_shop, other_shop.x, [("GRN-1", 1)])
    async with await client_for(shop.org_id, shop.x) as c:
        got = [o["order_number"] for o in (await c.get("/api/orders")).json()["orders"]]
    assert got == [shop.number(mine)]
    assert shop.number(theirs) not in got and other_shop.number(foreign) not in got
    async with await client_for(shop.org_id, shop.y) as c:
        got_y = [o["order_number"] for o in (await c.get("/api/orders")).json()["orders"]]
    assert got_y == [shop.number(theirs)]


async def test_bookings_endpoint_matches_the_tool(studio: Studio, owner_engine: AsyncEngine) -> None:
    await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", bs.local(2026, 11, 10, 14))
    await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", bs.local(2026, 11, 2, 11))
    await insert_studio_booking(
        owner_engine, studio, studio.x, "swedish", "bina", bs.local(2026, 11, 12, 11), status="cancelled"
    )
    with clock.freeze(bs.NOW):
        tool = bs.card(await bs.call(list_my_bookings, bs.turn_ctx(studio, studio.x)), "booking_list")
        async with await client_for(studio.org_id, studio.x) as c:
            r = await c.get("/api/bookings")
    assert r.status_code == 200
    assert r.json() == {"upcoming": tool["upcoming"], "past": tool["past"]}
    assert len(tool["upcoming"]) == 1 and len(tool["past"]) == 2


async def test_bookings_never_include_another_customers(
    studio: Studio, other_studio: Studio, owner_engine: AsyncEngine
) -> None:
    tue = bs.local(2026, 11, 10, 14)
    mine = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", tue)
    theirs = await insert_studio_booking(owner_engine, studio, studio.y, "swedish", "bina", tue)
    foreign = await insert_studio_booking(owner_engine, other_studio, other_studio.x, "swedish", "asha", tue)
    with clock.freeze(bs.NOW):
        async with await client_for(studio.org_id, studio.x) as c:
            body = (await c.get("/api/bookings")).json()
    numbers = [b["booking_number"] for b in body["upcoming"] + body["past"]]
    assert numbers == [studio.number(mine)]
    assert studio.number(theirs) not in numbers and other_studio.number(foreign) not in numbers


@pytest.mark.parametrize("path", ["/api/orders", "/api/bookings"])
async def test_panels_need_a_session(path: str) -> None:
    async with anonymous() as c:
        assert (await c.get(path)).status_code == 401


async def test_a_panel_the_orgs_vertical_lacks_is_the_uniform_404(shop: Shop, studio: Studio) -> None:
    async with await client_for(shop.org_id, shop.x) as c:
        no_bookings = await c.get("/api/bookings")
        missing = await c.get("/api/conversations/00000000-0000-0000-0000-000000000000/messages")
    async with await client_for(studio.org_id, studio.x) as c:
        no_orders = await c.get("/api/orders")
    assert no_bookings.status_code == no_orders.status_code == 404
    assert no_bookings.json() == no_orders.json() == missing.json()
