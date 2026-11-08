"""Every metric against numbers worked out by hand from tests/analytics/fixture.py.

Runs as app_readonly in a READ ONLY transaction scoped to the org's admin, exactly like the
admin tools. "Now" is Thu 12 Nov 2026 10:00 NPT.
"""

from datetime import date
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import TenantContext, readonly_tx
from app.domain.analytics.metrics import METRICS, compute
from app.domain.analytics.windows import resolve_window
from app.tenancy.orgs import get_org_by_id
from tests.analytics.fixture import NOW, build_shop, build_studio
from tests.factories import Shop, Studio

TZ = "Asia/Kathmandu"


async def metric(org_id: Any, admin: Any, name: str, window: str = "today", **kw: Any) -> dict[str, Any]:
    start, end = kw.pop("start_date", None), kw.pop("end_date", None)
    async with readonly_tx(TenantContext(org_id, admin, "admin")) as conn:
        org = await get_org_by_id(conn, org_id)
        assert org is not None
        return await compute(conn, org, name, resolve_window(window, TZ, NOW, start, end), **kw)


@pytest.fixture
async def built_shop(owner_engine: AsyncEngine, shop: Shop) -> Shop:
    await build_shop(owner_engine, shop)
    return shop


@pytest.fixture
async def built_studio(owner_engine: AsyncEngine, studio: Studio) -> Studio:
    await build_studio(owner_engine, studio)
    return studio


# ───────────── shape ─────────────


async def test_every_metric_states_window_and_definition(built_shop: Shop, built_studio: Studio) -> None:
    for name, m in METRICS.items():
        org = built_shop if "store" in m.verticals else built_studio
        r = await metric(org.org_id, org.admin, name, "this_week")
        assert set(r) == {"metric", "title", "values", "window", "definition"}
        assert r["window"]["timezone"] == TZ and r["window"]["start"] == "2026-11-09T00:00:00+05:45"
        assert r["definition"].endswith(".") and len(r["definition"]) > 30


async def test_a_metric_of_the_other_vertical_is_unavailable(built_shop: Shop) -> None:
    with pytest.raises(KeyError):
        await metric(built_shop.org_id, built_shop.admin, "practitioner_utilisation")


# ───────────── store ─────────────


async def test_orders_by_status_today(built_shop: Shop) -> None:
    v = (await metric(built_shop.org_id, built_shop.admin, "orders_by_status"))["values"]
    # o1 pending, o2 confirmed, o3 cancelled, o5 shipped (00:10 today); o4 at 23:50 is yesterday's.
    assert v == {"total": 4, "by_status": {"pending": 1, "confirmed": 1, "shipped": 1, "delivered": 0, "cancelled": 1}}


async def test_the_2350_order_counts_for_its_local_day(built_shop: Shop) -> None:
    v = (await metric(built_shop.org_id, built_shop.admin, "orders_by_status", "yesterday"))["values"]
    assert v["total"] == 1 and v["by_status"]["delivered"] == 1


@pytest.mark.parametrize(
    ("window", "amount", "orders"),
    [
        ("today", "4450.00", 3),  # o1 + o2 + o5; o3 is cancelled
        ("yesterday", "800.00", 1),  # o4
        ("this_week", "5250.00", 4),  # + o4
        ("this_month", "7650.00", 5),  # + o6; o7 (31 Oct 23:50 NPT) is last month
    ],
)
async def test_revenue(built_shop: Shop, window: str, amount: str, orders: int) -> None:
    v = (await metric(built_shop.org_id, built_shop.admin, "revenue", window))["values"]
    assert v["revenue"]["amount"] == amount and v["orders"] == orders


async def test_revenue_display_uses_the_org_currency(built_shop: Shop) -> None:
    v = (await metric(built_shop.org_id, built_shop.admin, "revenue"))["values"]
    assert v["revenue"]["display"] == "NPR 4,450.00"


async def test_average_order_value(built_shop: Shop) -> None:
    v = (await metric(built_shop.org_id, built_shop.admin, "average_order_value"))["values"]
    assert v["average_order_value"]["amount"] == "1483.33" and v["orders"] == 3  # 4450 / 3


async def test_average_order_value_with_no_orders_is_none(built_shop: Shop) -> None:
    r = await metric(built_shop.org_id, built_shop.admin, "average_order_value", "custom",
                     start_date=date(2026, 3, 1), end_date=date(2026, 3, 2))  # fmt: skip
    assert r["values"]["average_order_value"] is None and r["values"]["orders"] == 0


async def test_top_products(built_shop: Shop) -> None:
    today = (await metric(built_shop.org_id, built_shop.admin, "top_products"))["values"]["products"]
    assert [(p["sku"], p["units"]) for p in today] == [("BLK-3", 3), ("GRN-1", 2), ("GRN-2", 1)]
    month = (await metric(built_shop.org_id, built_shop.admin, "top_products", "this_month", limit=2))["values"][
        "products"
    ]
    assert [(p["sku"], p["units"]) for p in month] == [("GRN-1", 6), ("BLK-3", 3)]


async def test_most_out_of_stock(built_shop: Shop) -> None:
    today = (await metric(built_shop.org_id, built_shop.admin, "most_out_of_stock"))["values"]["products"]
    assert [(p["sku"], p["shortage_events"], p["units_short"]) for p in today] == [("BLK-2", 2, 3), ("BLK-1", 1, 2)]
    assert today[0]["name"] == "Black Beta" and today[0]["stock_now"] == 0
    week = (await metric(built_shop.org_id, built_shop.admin, "most_out_of_stock", "this_week"))["values"]["products"]
    assert [(p["sku"], p["shortage_events"], p["units_short"]) for p in week] == [("BLK-1", 2, 3), ("BLK-2", 2, 3)]


# ───────────── booking ─────────────


async def test_bookings_by_status_today(built_studio: Studio) -> None:
    v = (await metric(built_studio.org_id, built_studio.admin, "bookings_by_status"))["values"]
    assert v == {"total": 4, "by_status": {"confirmed": 1, "completed": 2, "cancelled": 1}}


async def test_cancellations(built_studio: Studio) -> None:
    today = (await metric(built_studio.org_id, built_studio.admin, "cancellations"))["values"]
    assert today == {"cancellations": 1, "with_fee": 1, "fees": {"amount": "1750.00", "display": "NPR 1,750.00"}}
    week = (await metric(built_studio.org_id, built_studio.admin, "cancellations", "this_week"))["values"]
    assert (week["cancellations"], week["with_fee"], week["fees"]["amount"]) == (2, 1, "1750.00")


async def test_practitioner_utilisation_today(built_studio: Studio) -> None:
    v = (await metric(built_studio.org_id, built_studio.admin, "practitioner_utilisation"))["values"]
    # Thursday: 09–19 = 600 open minutes. Asha 60 + 90; Bina 45 (b4 is cancelled).
    assert v["open_minutes"] == 600
    assert [(p["practitioner"], p["booked_minutes"], p["utilisation_pct"]) for p in v["practitioners"]] == [
        ("Asha Gurung", 150, "25.0"), ("Bina Shrestha", 45, "7.5")]  # fmt: skip


async def test_practitioner_utilisation_this_week_skips_holiday_and_closed_day(built_studio: Studio) -> None:
    v = (await metric(built_studio.org_id, built_studio.admin, "practitioner_utilisation", "this_week"))["values"]
    # Mon 600 + Tue 600 + Wed holiday 0 + Thu 600 + Fri 360 + Sat closed 0 + Sun 600 = 2760.
    assert v["open_minutes"] == 2760
    # Bina: 45 + 60 + 45 + 90 = 240; Asha: 60 + 90 = 150 (b6 cancelled, b8 last week).
    assert [(p["practitioner"], p["booked_minutes"], p["utilisation_pct"]) for p in v["practitioners"]] == [
        ("Bina Shrestha", 240, "8.7"), ("Asha Gurung", 150, "5.4")]  # fmt: skip


async def test_top_services_this_week(built_studio: Studio) -> None:
    v = (await metric(built_studio.org_id, built_studio.admin, "top_services", "this_week"))["values"]["services"]
    # Two each; ties broken by revenue: deep 10400, swedish 7000, yoga 4400.
    assert [(s["service"], s["bookings"], s["revenue"]["amount"]) for s in v] == [
        ("Deep Tissue Massage", 2, "10400.00"), ("Swedish Massage", 2, "7000.00"), ("Private Yoga", 2, "4400.00")]  # fmt: skip


async def test_busiest_slots_this_week(built_studio: Studio) -> None:
    v = (await metric(built_studio.org_id, built_studio.admin, "busiest_slots", "this_week"))["values"]["slots"]
    assert [(s["weekday"], s["hour"], s["bookings"]) for s in v] == [
        ("Thursday", "09:00", 2), ("Monday", "09:00", 1), ("Tuesday", "10:00", 1),
        ("Thursday", "14:00", 1), ("Friday", "09:00", 1)]  # fmt: skip


# ───────────── both ─────────────


async def test_active_customers(built_shop: Shop) -> None:
    get = lambda w: metric(built_shop.org_id, built_shop.admin, "active_customers", w)  # noqa: E731
    assert (await get("today"))["values"] == {"active_customers": 2}  # x, y; the admin doesn't count
    assert (await get("yesterday"))["values"] == {"active_customers": 1}  # y at 23:45


@pytest.mark.parametrize(("window", "n"), [("today", 1), ("this_week", 2), ("last_7_days", 3)])
async def test_new_signups(built_studio: Studio, window: str, n: int) -> None:
    v = (await metric(built_studio.org_id, built_studio.admin, "new_signups", window))["values"]
    assert v == {"new_signups": n}


async def test_conversion_rate_store(built_shop: Shop) -> None:
    today = (await metric(built_shop.org_id, built_shop.admin, "conversion_rate"))["values"]
    # c1, c2, c3 started today; c1 and c2 have orders. c_old's order (o5, today) doesn't
    # count: the conversation started on 1 Nov. The admin chat isn't a customer conversation.
    assert today == {"conversations": 3, "converted": 2, "conversion_rate_pct": "66.7"}
    week = (await metric(built_shop.org_id, built_shop.admin, "conversion_rate", "this_week"))["values"]
    assert week == {"conversations": 4, "converted": 3, "conversion_rate_pct": "75.0"}


async def test_conversion_rate_booking(built_studio: Studio) -> None:
    v = (await metric(built_studio.org_id, built_studio.admin, "conversion_rate"))["values"]
    assert v == {"conversations": 2, "converted": 1, "conversion_rate_pct": "50.0"}


async def test_conversion_rate_with_no_conversations_is_none(built_shop: Shop) -> None:
    v = (await metric(built_shop.org_id, built_shop.admin, "conversion_rate", "custom",
                      start_date=date(2026, 3, 1), end_date=date(2026, 3, 1)))["values"]  # fmt: skip
    assert v == {"conversations": 0, "converted": 0, "conversion_rate_pct": None}


async def test_ai_usage_today(built_shop: Shop) -> None:
    v = (await metric(built_shop.org_id, built_shop.admin, "ai_usage"))["values"]
    assert (v["runs"], v["input_tokens"], v["output_tokens"]) == (4, 5500, 550)
    # 3.5: 2000 in, 400 out → 0.0002 + 0.00016; 3.1: 3000 / 100 → 0.0003 + 0.00004.
    by = {m["model"]: m for m in v["by_model"]}
    assert by["gemini-3.5-flash-lite"]["estimated_cost_usd"] == "0.000360"
    assert by["gemini-3.1-flash-lite"]["estimated_cost_usd"] == "0.000340"
    assert by["unknown-model"]["estimated_cost_usd"] is None
    assert v["estimated_cost_usd"] == "0.000700" and v["unpriced_models"] == ["unknown-model"]
    assert "free tier actual cost $0" in v["pricing_note"]


async def test_utilisation_on_a_closed_day_says_so_instead_of_a_rate(built_studio: Studio) -> None:
    """Saturday is closed: no open minutes, so no rate (null, never 0%), and the result says why."""
    v = (await metric(built_studio.org_id, built_studio.admin, "practitioner_utilisation", "custom",
                      start_date=date(2026, 11, 14), end_date=date(2026, 11, 14)))["values"]  # fmt: skip
    assert v["open_minutes"] == 0
    assert all(p["utilisation_pct"] is None for p in v["practitioners"])
    assert "closed" in v["note"] and "not 0%" in v["note"]


async def test_utilisation_has_no_note_when_open(built_studio: Studio) -> None:
    v = (await metric(built_studio.org_id, built_studio.admin, "practitioner_utilisation", "this_week"))["values"]
    assert "note" not in v
