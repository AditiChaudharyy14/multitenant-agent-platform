"""create_order and confirm_proposal: the proposal rules, enforced below the LLM.

Tool handlers are called directly with a server-built ToolContext, as app_user.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock
from app.agents.tools.common.confirm_proposal import confirm_proposal
from app.agents.tools.store import create_order
from app.domain.proposals.service import PROPOSAL_TTL
from tests.factories import Shop, insert_shop_order
from tests.store_support import (
    call, card, history, order_by_number, order_items, proposal_id, proposal_status, rows,
    scalar, set_price, set_stock as set_stock_to, stock, turn_ctx,
)  # fmt: skip

pytestmark = pytest.mark.usefixtures("as_app_user")

ADDRESS = "House 7, Baluwatar, Kathmandu"


async def order_count(owner: AsyncEngine, shop: Shop) -> int:
    return await scalar(owner, shop, "SELECT count(*) FROM orders WHERE org_id = :o")


async def propose(shop: Shop, user: uuid.UUID | None = None, items=None, **extra):
    user = user or shop.x
    ctx = turn_ctx(shop, user)
    result = await call(create_order, ctx, items=items or [{"sku": "GRN-1", "qty": 2}],
                        delivery_address=ADDRESS, **extra)  # fmt: skip
    return ctx, result


async def test_create_order_only_proposes(shop: Shop, owner_engine: AsyncEngine) -> None:
    _, result = await propose(shop, items=[{"sku": "GRN-1", "qty": 2}, {"sku": "BLK-3", "qty": 1}])

    summary = card(result, "order_summary")
    assert summary["kind"] == "create_order"
    assert [(line["sku"], line["qty"], line["unit_price"], line["line_total"]) for line in summary["lines"]] == [
        ("GRN-1", 2, "800.00", "1600.00"), ("BLK-3", 1, "650.00", "650.00"),
    ]  # fmt: skip
    assert summary["total"] == "2250.00"
    assert summary["total_display"] == "NPR 2,250.00"
    assert summary["delivery_address"] == ADDRESS
    assert summary["payment_method"] == "Cash on delivery"
    assert summary["actions"] == [{"type": "confirm_proposal", "proposal_id": summary["proposal_id"], "label": "Confirm order"}]
    # Nothing committed: no order, stock untouched, one open proposal.
    assert await order_count(owner_engine, shop) == 0
    assert await stock(owner_engine, shop, "GRN-1") == 10
    assert await proposal_status(owner_engine, shop, proposal_id(result)) == "open"


async def test_confirm_in_same_turn_refused_then_next_turn_commits(shop: Shop, owner_engine: AsyncEngine) -> None:
    ctx, result = await propose(shop)
    pid = str(proposal_id(result))

    same_turn = await call(confirm_proposal, ctx, proposal_id=pid)
    assert "REFUSED" in same_turn.content_for_model
    assert await order_count(owner_engine, shop) == 0
    assert await proposal_status(owner_engine, shop, uuid.UUID(pid)) == "open"

    next_turn = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=pid)
    placed = card(next_turn, "order_confirmed")
    number = int(placed["order_number"].split("-")[1])
    assert placed["order_number"] == shop.number(number)
    assert placed["status"] == "pending"

    order = await order_by_number(owner_engine, shop, number)
    assert order.status == "pending"
    assert order.user_id == shop.x
    assert order.conversation_id == shop.conversations[shop.x]
    assert order.proposal_id == uuid.UUID(pid)
    assert order.total == Decimal("1600.00")
    assert order.payment_method == "cod"
    assert await order_items(owner_engine, shop, number) == {"GRN-1": 2}
    assert await stock(owner_engine, shop, "GRN-1") == 8
    assert await history(owner_engine, shop, number) == [(None, "pending", "customer")]
    assert await proposal_status(owner_engine, shop, uuid.UUID(pid)) == "confirmed"


async def test_first_order_number_is_1001_and_increments(shop: Shop) -> None:
    numbers = []
    for _ in range(2):
        _, result = await propose(shop)
        done = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=str(proposal_id(result)))
        numbers.append(card(done, "order_confirmed")["order_number"])
    assert numbers == [shop.number(1001), shop.number(1002)]


async def test_double_confirm_creates_one_order(shop: Shop, owner_engine: AsyncEngine) -> None:
    _, result = await propose(shop)
    pid = str(proposal_id(result))
    first = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=pid)
    second = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=pid)

    card(first, "order_confirmed")
    assert "REFUSED" in second.content_for_model and "already confirmed" in second.content_for_model
    assert await order_count(owner_engine, shop) == 1
    assert await stock(owner_engine, shop, "GRN-1") == 8


async def test_already_confirmed_refusal_names_the_order_and_its_current_status(shop: Shop, owner_engine: AsyncEngine) -> None:
    """Seen in a live eval: told only "already confirmed", the model said a pending order was
    "on its way". The refusal must carry the order number and its live status."""
    _, result = await propose(shop)
    pid = str(proposal_id(result))
    first = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=pid)
    number = card(first, "order_confirmed")["order_number"]

    again = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=pid)
    assert "already confirmed" in again.content_for_model
    assert number in again.content_for_model and "status: pending" in again.content_for_model

    # The status is read at refusal time, not copied from when it was confirmed.
    await scalar(owner_engine, shop, "UPDATE orders SET status = 'shipped' WHERE proposal_id = :p RETURNING 1", p=pid)
    later = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=pid)
    assert number in later.content_for_model and "status: shipped" in later.content_for_model
    assert await order_count(owner_engine, shop) == 1


async def test_price_change_supersedes_and_commits_nothing(shop: Shop, owner_engine: AsyncEngine) -> None:
    _, result = await propose(shop)
    old = proposal_id(result)
    await set_price(owner_engine, shop, "GRN-1", "950.00")

    outcome = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=str(old))

    fresh = card(outcome, "order_summary")
    assert uuid.UUID(fresh["proposal_id"]) != old
    assert fresh["lines"][0]["unit_price"] == "950.00"
    assert fresh["total"] == "1900.00"
    assert any("800.00" in c and "950.00" in c for c in fresh["changes"]), fresh["changes"]
    assert "CHANGED" in outcome.content_for_model
    assert await proposal_status(owner_engine, shop, old) == "superseded"
    assert await proposal_status(owner_engine, shop, uuid.UUID(fresh["proposal_id"])) == "open"
    assert await order_count(owner_engine, shop) == 0
    assert await stock(owner_engine, shop, "GRN-1") == 10


async def test_fresh_proposal_after_change_must_wait_for_next_turn(shop: Shop, owner_engine: AsyncEngine) -> None:
    """The re-issued proposal belongs to the confirming turn, so that turn can't confirm it."""
    _, result = await propose(shop)
    await set_price(owner_engine, shop, "GRN-1", "950.00")
    confirming = turn_ctx(shop, shop.x)
    outcome = await call(confirm_proposal, confirming, proposal_id=str(proposal_id(result)))
    fresh = card(outcome, "order_summary")["proposal_id"]

    again = await call(confirm_proposal, confirming, proposal_id=fresh)
    assert "REFUSED" in again.content_for_model
    assert await order_count(owner_engine, shop) == 0

    done = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=fresh)
    assert card(done, "order_confirmed")["total"] == "1900.00"


async def test_stock_drop_before_confirm_supersedes(shop: Shop, owner_engine: AsyncEngine) -> None:
    _, result = await propose(shop, items=[{"sku": "GRN-2", "qty": 5}])
    await set_stock_to(owner_engine, shop, "GRN-2", 3)

    outcome = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=str(proposal_id(result)))

    assert "CHANGED" in outcome.content_for_model
    assert await proposal_status(owner_engine, shop, proposal_id(result)) == "superseded"
    assert await order_count(owner_engine, shop) == 0
    assert await stock(owner_engine, shop, "GRN-2") == 3
    # Short now, so no fresh proposal: the customer gets availability and alternatives.
    issue = card(outcome, "stock_issue")
    assert issue["lines"][0]["sku"] == "GRN-2" and issue["lines"][0]["available"] == 3


async def test_qty_over_stock_gives_alternatives_and_no_proposal(shop: Shop, owner_engine: AsyncEngine) -> None:
    _, result = await propose(shop, items=[{"sku": "BLK-1", "qty": 3}, {"sku": "GRN-1", "qty": 1}])

    assert not [c for c in result.cards if c["type"] == "order_summary"]
    issue = card(result, "stock_issue")
    [line] = issue["lines"]
    assert (line["sku"], line["requested"], line["available"]) == ("BLK-1", 3, 1)
    # Same category, in stock for the requested quantity, nearest price first (600 → 650, 700 is sold out).
    assert [a["sku"] for a in line["alternatives"]] == ["BLK-3"]
    assert line["alternatives"][0]["price_display"] == "NPR 650.00"
    assert "BLK-3" in result.content_for_model
    assert await rows(owner_engine, shop, "SELECT id FROM proposals WHERE org_id = :o") == []
    assert await order_count(owner_engine, shop) == 0


async def test_out_of_stock_alternatives_nearest_price(shop: Shop) -> None:
    _, result = await propose(shop, items=[{"sku": "GRN-3", "qty": 4}])  # 1500, stock 3
    [line] = card(result, "stock_issue")["lines"]
    # GRN-2 (900) is nearer to 1500 than GRN-1 (800); both have 4+ in stock.
    assert [a["sku"] for a in line["alternatives"]] == ["GRN-2", "GRN-1"]


async def test_unknown_sku_is_an_error(shop: Shop, owner_engine: AsyncEngine) -> None:
    _, result = await propose(shop, items=[{"sku": "NOPE-9", "qty": 1}])
    assert not result.ok
    assert "NOPE-9" in result.content_for_model
    assert await rows(owner_engine, shop, "SELECT id FROM proposals WHERE org_id = :o") == []


async def test_address_defaults_to_most_recent_order(shop: Shop, owner_engine: AsyncEngine) -> None:
    from datetime import UTC, datetime

    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status="delivered",
                            address="Old Road 1", created_at=datetime(2026, 10, 1, tzinfo=UTC))  # fmt: skip
    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status="delivered",
                            address="New Road 2", created_at=datetime(2026, 10, 20, tzinfo=UTC))  # fmt: skip
    result = await call(create_order, turn_ctx(shop, shop.x), items=[{"sku": "GRN-1", "qty": 1}])
    assert card(result, "order_summary")["delivery_address"] == "New Road 2"


async def test_no_address_and_no_history_asks_for_one(shop: Shop, owner_engine: AsyncEngine) -> None:
    result = await call(create_order, turn_ctx(shop, shop.y), items=[{"sku": "GRN-1", "qty": 1}])
    assert "address" in result.content_for_model.lower()
    assert not [c for c in result.cards if c["type"] == "order_summary"]
    assert await rows(owner_engine, shop, "SELECT id FROM proposals WHERE org_id = :o") == []


async def test_expired_proposal_refused(shop: Shop, owner_engine: AsyncEngine) -> None:
    _, result = await propose(shop)
    later = clock.now() + PROPOSAL_TTL + timedelta(seconds=1)
    with clock.freeze(later):
        outcome = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=str(proposal_id(result)))
    assert "REFUSED" in outcome.content_for_model and "expired" in outcome.content_for_model
    assert await proposal_status(owner_engine, shop, proposal_id(result)) == "expired"
    assert await order_count(owner_engine, shop) == 0


async def test_proposal_ttl_is_15_minutes() -> None:
    assert PROPOSAL_TTL == timedelta(minutes=15)


async def test_new_proposal_supersedes_older_open_one(shop: Shop, owner_engine: AsyncEngine) -> None:
    _, first = await propose(shop)
    _, second = await propose(shop, items=[{"sku": "BLK-3", "qty": 1}])
    assert await proposal_status(owner_engine, shop, proposal_id(first)) == "superseded"
    assert await proposal_status(owner_engine, shop, proposal_id(second)) == "open"

    outcome = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=str(proposal_id(first)))
    assert "REFUSED" in outcome.content_for_model and "superseded" in outcome.content_for_model
    assert await order_count(owner_engine, shop) == 0


async def test_confirm_from_another_conversation_is_not_found(shop: Shop, owner_engine: AsyncEngine) -> None:
    from tests.factories import set_owner_tenant
    from sqlalchemy import text

    _, result = await propose(shop)
    async with owner_engine.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        other = await conn.scalar(text("INSERT INTO conversations (org_id, user_id) VALUES (:o, :u) RETURNING id"),
                                  {"o": shop.org_id, "u": shop.x})  # fmt: skip
    outcome = await call(confirm_proposal, turn_ctx(shop, shop.x, conversation=other),
                         proposal_id=str(proposal_id(result)))  # fmt: skip
    assert "NOT FOUND" in outcome.content_for_model
    assert await order_count(owner_engine, shop) == 0


async def test_confirm_garbage_id_is_not_found(shop: Shop) -> None:
    outcome = await call(confirm_proposal, turn_ctx(shop, shop.x), proposal_id=str(uuid.uuid4()))
    assert "NOT FOUND" in outcome.content_for_model
