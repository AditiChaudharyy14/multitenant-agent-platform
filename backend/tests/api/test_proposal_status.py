"""GET /api/conversations/{id}/proposals: the current status of each proposal in a chat,
so cards loaded from history show Confirm, Confirmed, or greyed-out correctly."""

from datetime import timedelta

import pytest

from app import clock
from app.agents.tools.store import create_order
from app.domain.proposals import service as proposals
from app.domain.proposals.repo import PROPOSAL_TTL
from tests import store_support as ss
from tests.api_support import client_for
from tests.factories import Shop

pytestmark = pytest.mark.usefixtures("as_app_user")


async def propose(shop: Shop, user, qty: int = 1):  # noqa: ANN001, ANN201
    r = await ss.call(create_order, ss.turn_ctx(shop, user), items=[{"sku": "GRN-1", "qty": qty}], delivery_address="House 7")
    return ss.proposal_id(r)


async def statuses(shop: Shop, user) -> dict[str, str]:  # noqa: ANN001
    async with await client_for(shop.org_id, user) as c:
        r = await c.get(f"/api/conversations/{shop.conversations[user]}/proposals")
    assert r.status_code == 200, r.text
    return {p["proposal_id"]: p["status"] for p in r.json()}


async def test_open_then_superseded_then_confirmed(shop: Shop) -> None:
    first = await propose(shop, shop.x)
    assert await statuses(shop, shop.x) == {str(first): "open"}
    second = await propose(shop, shop.x, qty=2)  # a newer summary supersedes the older one
    assert await statuses(shop, shop.x) == {str(first): "superseded", str(second): "open"}
    reply = await proposals.confirm(ss.turn_ctx(shop, shop.x), second)  # a later turn
    assert reply.status == "committed"
    assert await statuses(shop, shop.x) == {str(first): "superseded", str(second): "confirmed"}


async def test_open_past_its_expiry_reads_as_expired(shop: Shop) -> None:
    pid = await propose(shop, shop.x)
    with clock.freeze(clock.now() + PROPOSAL_TTL + timedelta(minutes=1)):
        assert await statuses(shop, shop.x) == {str(pid): "expired"}
    assert await statuses(shop, shop.x) == {str(pid): "open"}  # reading changed nothing


async def test_another_customers_conversation_is_the_uniform_404(shop: Shop) -> None:
    await propose(shop, shop.x)
    async with await client_for(shop.org_id, shop.y) as c:
        foreign = await c.get(f"/api/conversations/{shop.conversations[shop.x]}/proposals")
        missing = await c.get("/api/conversations/00000000-0000-0000-0000-000000000000/proposals")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert await statuses(shop, shop.y) == {}  # Y's own chat has none


async def test_needs_a_session(shop: Shop) -> None:
    from tests.api_support import anonymous

    async with anonymous() as c:
        assert (await c.get(f"/api/conversations/{shop.conversations[shop.x]}/proposals")).status_code == 401
