"""activity_summary: the broad "how did we do?" answer in one read-only call.

It must equal the individual metrics exactly (it reuses them, never re-derives a number),
pick its sections by org.vertical, and give every section its window and definition.
"""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock
from app.agents.tools.admin import ADMIN_TOOLS
from app.db.session import TenantContext, readonly_tx
from app.domain.analytics.metrics import METRICS, SUMMARY_SECTIONS, activity_summary
from app.domain.analytics.windows import resolve_window
from app.tenancy.context import ToolContext
from app.tenancy.orgs import get_org_by_id
from tests.analytics.fixture import NOW, build_shop, build_studio
from tests.analytics.test_metrics import TZ, metric
from tests.factories import Shop, Studio
from tests.store_support import call


@pytest.fixture
async def built_shop(owner_engine: AsyncEngine, shop: Shop) -> Shop:
    await build_shop(owner_engine, shop)
    return shop


@pytest.fixture
async def built_studio(owner_engine: AsyncEngine, studio: Studio) -> Studio:
    await build_studio(owner_engine, studio)
    return studio


async def summary(org_id: Any, admin: Any, window: str = "today") -> dict[str, Any]:
    async with readonly_tx(TenantContext(org_id, admin, "admin")) as conn:
        org = await get_org_by_id(conn, org_id)
        assert org is not None
        return await activity_summary(conn, org, resolve_window(window, TZ, NOW, None, None))


def test_sections_are_keyed_on_vertical_and_exist_as_metrics() -> None:
    assert set(SUMMARY_SECTIONS) == {"store", "booking"}
    for vertical, names in SUMMARY_SECTIONS.items():
        for n in names:
            assert vertical in METRICS[n].verticals, (vertical, n)
    assert {"orders_by_status", "revenue", "average_order_value"} <= set(SUMMARY_SECTIONS["store"])
    assert {"bookings_by_status", "cancellations"} <= set(SUMMARY_SECTIONS["booking"])
    for names in SUMMARY_SECTIONS.values():
        assert {"active_customers", "new_signups", "conversion_rate"} <= set(names)


@pytest.mark.parametrize("window", ["today", "this_week"])
async def test_store_summary_equals_the_individual_metrics(built_shop: Shop, window: str) -> None:
    s = await summary(built_shop.org_id, built_shop.admin, window)
    assert s["metric"] == "activity_summary" and s["window"]["timezone"] == TZ
    assert [sec["metric"] for sec in s["sections"]] == SUMMARY_SECTIONS["store"]
    for sec in s["sections"]:
        alone = await metric(built_shop.org_id, built_shop.admin, sec["metric"], window)
        assert sec == alone  # same values, same window, same definition


async def test_booking_summary_equals_the_individual_metrics(built_studio: Studio) -> None:
    s = await summary(built_studio.org_id, built_studio.admin, "this_week")
    assert [sec["metric"] for sec in s["sections"]] == SUMMARY_SECTIONS["booking"]
    by = {sec["metric"]: sec for sec in s["sections"]}
    assert by["cancellations"]["values"]["cancellations"] == 2
    for sec in s["sections"]:
        assert sec == await metric(built_studio.org_id, built_studio.admin, sec["metric"], "this_week")


async def test_every_section_states_its_definition(built_shop: Shop) -> None:
    s = await summary(built_shop.org_id, built_shop.admin)
    assert s["definition"].endswith(".")
    for sec in s["sections"]:
        assert sec["definition"] == METRICS[sec["metric"]].definition


async def test_tool_returns_one_metric_card_per_section(built_shop: Shop) -> None:
    ctx = ToolContext(built_shop.org_id, built_shop.admin, "admin", uuid.uuid4(), uuid.uuid4())
    with clock.freeze(NOW):
        result = await call(ADMIN_TOOLS["activity_summary"], ctx, window="today")
    assert result.ok, result.content_for_model
    body = json.loads(result.content_for_model)
    assert [c["metric"] for c in result.cards] == SUMMARY_SECTIONS["store"]
    assert all(c["type"] == "metric" for c in result.cards)
    revenue = next(sec for sec in body["sections"] if sec["metric"] == "revenue")
    assert revenue["values"]["revenue"]["amount"] == "4450.00"  # same as test_revenue[today]
