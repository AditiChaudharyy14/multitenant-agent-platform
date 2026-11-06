"""POST /api/admin/orders/{order_number}/advance: admin only, one step at a time.

pending → confirmed → shipped → delivered, each with a history row by 'admin'. Delivered
and cancelled orders can't advance. A customer gets 403; a foreign order gets 404.
"""

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth.sessions import COOKIE_NAME, create_session
from app.db.session import app_engine
from app.main import app
from tests.factories import Shop, insert_shop_order
from tests.store_support import history, order_by_number

pytestmark = pytest.mark.usefixtures("as_app_user")

ORIGIN = "http://localhost:3000"


async def client_for(shop: Shop, user, role: str) -> httpx.AsyncClient:
    """A logged-in client: the session row is created exactly like a login would (as app_user)."""
    async with app_engine.begin() as conn:
        token = await create_session(conn, shop.org_id, user, role)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Origin": ORIGIN},
                             cookies={COOKIE_NAME: token})  # fmt: skip


async def test_advance_through_every_status(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)])
    async with await client_for(shop, shop.admin, "admin") as c:
        seen = []
        for _ in range(3):
            r = await c.post(f"/api/admin/orders/{shop.number(n)}/advance")
            assert r.status_code == 200, r.text
            seen.append((r.json()["from_status"], r.json()["status"]))
        assert seen == [("pending", "confirmed"), ("confirmed", "shipped"), ("shipped", "delivered")]

        r = await c.post(f"/api/admin/orders/{shop.number(n)}/advance")
        assert r.status_code == 409
        assert "delivered" in r.json()["detail"]

    assert (await order_by_number(owner_engine, shop, n)).status == "delivered"
    assert [h[1:] for h in (await history(owner_engine, shop, n))[1:]] == [
        ("confirmed", "admin"), ("shipped", "admin"), ("delivered", "admin"),
    ]  # fmt: skip


async def test_cancelled_order_cannot_advance(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status="cancelled")
    async with await client_for(shop, shop.admin, "admin") as c:
        r = await c.post(f"/api/admin/orders/{shop.number(n)}/advance")
    assert r.status_code == 409 and "cancelled" in r.json()["detail"]


async def test_customer_gets_403_and_nothing_changes(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)])
    async with await client_for(shop, shop.x, "customer") as c:
        r = await c.post(f"/api/admin/orders/{shop.number(n)}/advance")
    assert r.status_code == 403
    assert (await order_by_number(owner_engine, shop, n)).status == "pending"


async def test_other_org_or_missing_order_is_identical_404(shop: Shop, other_shop: Shop, owner_engine: AsyncEngine) -> None:
    theirs = await insert_shop_order(owner_engine, other_shop, other_shop.x, [("GRN-1", 1)])
    async with await client_for(shop, shop.admin, "admin") as c:
        responses = [
            await c.post(f"/api/admin/orders/{other_shop.number(theirs)}/advance"),
            await c.post(f"/api/admin/orders/{shop.number(theirs)}/advance"),
            await c.post(f"/api/admin/orders/{shop.number(424242)}/advance"),
            await c.post("/api/admin/orders/garbage/advance"),
        ]
    assert {r.status_code for r in responses} == {404}
    assert len({r.text for r in responses}) == 1
    assert (await order_by_number(owner_engine, other_shop, theirs)).status == "pending"


async def test_unauthenticated_is_401(shop: Shop) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Origin": ORIGIN}) as c:
        r = await c.post(f"/api/admin/orders/{shop.number(1001)}/advance")
    assert r.status_code == 401
