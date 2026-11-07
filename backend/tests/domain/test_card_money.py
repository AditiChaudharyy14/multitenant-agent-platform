"""Every money value a card shows comes from format_money ("NPR 1,500.00"), line prices included."""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.tools.store import create_order, get_order_status
from app.domain.proposals import service as proposals
from tests import store_support as ss
from tests.factories import Shop

pytestmark = pytest.mark.usefixtures("as_app_user")


def assert_formatted(line: dict) -> None:
    assert line["unit_price_display"].startswith("NPR ") and line["line_total_display"].startswith("NPR ")
    assert "," in line["line_total_display"] or float(line["line_total"]) < 1000  # thousands separated
    assert line["line_total_display"] == f"NPR {float(line['line_total']):,.2f}"


async def test_summary_confirmed_and_status_cards_format_line_prices(shop: Shop, owner_engine: AsyncEngine) -> None:
    ctx = ss.turn_ctx(shop, shop.x)
    made = await ss.call(create_order, ctx, items=[{"sku": "GRN-1", "qty": 3}], delivery_address="House 7")
    summary = ss.card(made, "order_summary")
    assert_formatted(summary["lines"][0])
    reply = await proposals.confirm(ss.turn_ctx(shop, shop.x), ss.proposal_id(made))
    assert_formatted(reply.cards[0]["lines"][0])  # order_confirmed
    number = reply.cards[0]["order_number"]
    status = ss.card(await ss.call(get_order_status, ss.turn_ctx(shop, shop.x), order_number=number), "order_status")
    assert_formatted(status["items"][0])
