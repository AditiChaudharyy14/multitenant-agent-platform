"""Live facts must be re-fetched each time they are stated, never repeated from earlier.

Prices, stock, order and booking status and availability can change between turns (an
admin advances an order, another customer takes the slot, a price moves). The system
prompt says so, and every tool that returns such a fact says so in its description.
"""

from datetime import UTC, datetime

import pytest

from app.agents.prompts.system import FRESHNESS_RULE, build_system_prompt
from app.agents.tools.base import FRESHNESS
from app.agents.tools.registry import tools_for
from app.tenancy.orgs import Org

LIVE_TOOLS = {
    "store": {"search_products", "get_order_status", "list_my_orders"},
    "booking": {"list_services", "get_opening_hours", "find_available_slots", "list_my_bookings"},
}


def org(vertical: str) -> Org:
    return Org(id=__import__("uuid").uuid4(), slug="x", name="X", vertical=vertical, timezone="Asia/Kathmandu",
               branding={}, order_prefix="XX" if vertical == "store" else None,
               booking_prefix="XB" if vertical == "booking" else None)  # fmt: skip


@pytest.mark.parametrize("vertical", ["store", "booking"])
def test_the_system_prompt_requires_re_fetching_live_facts(vertical: str) -> None:
    prompt = build_system_prompt(org(vertical), datetime(2026, 11, 9, 4, 15, tzinfo=UTC))
    assert FRESHNESS_RULE in prompt
    for fact in ("prices", "stock", "status", "availability"):
        assert fact in FRESHNESS_RULE.lower()
    assert "earlier in the conversation" in FRESHNESS_RULE


def test_tools_that_only_propose_are_not_marked_live() -> None:
    assert (
        not tools_for("store")["create_order"].live
        and FRESHNESS not in tools_for("store")["create_order"].spec().description
    )


@pytest.mark.parametrize("vertical", ["store", "booking"])
def test_every_live_data_tool_says_to_call_it_again(vertical: str) -> None:
    tools = tools_for(vertical)
    assert LIVE_TOOLS[vertical] <= set(tools)
    for name in LIVE_TOOLS[vertical]:
        assert FRESHNESS in tools[name].spec().description, name  # what the model sees
