"""The admin analytics agent can only read, and only its own org.

- Its registry holds only the metric tools: nothing that proposes, confirms or writes.
- The tools' connection is app_readonly in a READ ONLY transaction: a write raises.
- RLS scopes it to the admin's org: a store admin sees none of another org's rows.
- Admin conversations are invisible to (and can't be created by) a non-admin context.
"""

import json
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock
from app.agents.tools.admin import ADMIN_TOOLS
from app.agents.tools.registry import ADMIN_REGISTRY, REGISTRY, admin_tools_for
from app.db.session import TenantContext, readonly_tx, tenant_tx
from app.domain.analytics.metrics import METRICS
from app.tenancy.context import ToolContext
from tests.analytics.fixture import NOW, build_shop, build_studio
from tests.factories import Shop, Studio, pg_cause, set_owner_tenant
from tests.store_support import call

pytestmark = pytest.mark.usefixtures("as_app_user")

MUTATING = {"create_order", "update_order", "cancel_order", "reorder", "create_booking", "reschedule_booking",
            "cancel_booking", "confirm_proposal"}  # fmt: skip
IDENTITY = {"org_id", "user_id", "customer_id", "role"}


# ───────────── registry ─────────────


@pytest.mark.parametrize("vertical", list(REGISTRY))
def test_admin_registry_holds_only_metric_tools(vertical: str) -> None:
    names = set(admin_tools_for(vertical))
    # One tool per metric of the vertical, plus activity_summary (which only composes them).
    assert names and names <= set(METRICS) | {"activity_summary"}
    assert not names & MUTATING and "search_knowledge" not in names
    assert names == {n for n, m in METRICS.items() if vertical in m.verticals} | {"activity_summary"}  # keyed on vertical


@pytest.mark.parametrize("vertical", list(REGISTRY))
def test_customer_registry_has_no_admin_tools(vertical: str) -> None:
    assert not set(t.name for t in REGISTRY[vertical]) & set(ADMIN_TOOLS)


def test_admin_tool_schemas_never_carry_identity() -> None:
    for tools in ADMIN_REGISTRY.values():
        for t in tools:
            props = set(t.spec().parameters.get("properties", {}))
            assert not props & IDENTITY, (t.name, props)
            assert "window" in props
            with pytest.raises(ValueError):  # extra="forbid": the model can't sneak one in
                t.args_model.model_validate({"window": "today", "org_id": str(uuid.uuid4())})


# ───────────── READ ONLY, as app_readonly ─────────────


async def test_readonly_tx_is_app_readonly_and_read_only(shop: Shop) -> None:
    async with readonly_tx(TenantContext(shop.org_id, shop.admin, "admin")) as conn:
        who, ro, org = (await conn.execute(text(
            "SELECT current_user, current_setting('transaction_read_only'), current_setting('app.org_id')"))).one()  # fmt: skip
        flags = (await conn.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))).one()
    assert (who, ro, org) == ("app_readonly", "on", str(shop.org_id))
    assert tuple(flags) == (False, False)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE orders SET status = 'cancelled'",
        "DELETE FROM orders",
        "INSERT INTO stock_events (org_id, user_id, sku, requested_qty, available_qty) VALUES (app_org(), app_uid(), 'X', 2, 1)",
        "UPDATE products SET stock = 0",
        "CREATE TABLE sneaky (id int)",
    ],
)
async def test_a_write_through_the_admin_connection_raises(shop: Shop, owner_engine: AsyncEngine, sql: str) -> None:
    with pytest.raises(DBAPIError) as ei:
        async with readonly_tx(TenantContext(shop.org_id, shop.admin, "admin")) as conn:
            await conn.execute(text(sql))
    assert isinstance(pg_cause(ei), asyncpg.ReadOnlySQLTransactionError)


async def test_app_readonly_has_no_write_grant_even_outside_read_only(shop: Shop) -> None:
    """Defence in depth: without READ ONLY the role still lacks INSERT/UPDATE/DELETE."""
    from app.db.session import readonly_engine

    with pytest.raises(DBAPIError) as ei:
        async with readonly_engine.begin() as conn:  # deliberately not readonly_tx
            await conn.execute(text("SELECT set_config('app.org_id', :o, true), set_config('app.role', 'admin', true)"),
                               {"o": str(shop.org_id)})  # fmt: skip
            await conn.execute(text("UPDATE orders SET status = 'cancelled'"))
    assert isinstance(pg_cause(ei), asyncpg.InsufficientPrivilegeError)


# ───────────── org scoping ─────────────


async def _tool_values(org_id: uuid.UUID, admin: uuid.UUID, name: str, window: str = "this_month") -> dict:
    ctx = ToolContext(org_id, admin, "admin", uuid.uuid4(), uuid.uuid4())
    with clock.freeze(NOW):
        result = await call(ADMIN_TOOLS[name], ctx, window=window)
    assert result.ok, result.content_for_model
    assert result.cards[0]["type"] == "metric"
    return json.loads(result.content_for_model)["values"]


async def test_store_admin_sees_zero_rows_of_another_org(shop: Shop, other_shop: Shop, owner_engine: AsyncEngine) -> None:
    await build_shop(owner_engine, other_shop)  # all the activity is in the other org
    async with owner_engine.begin() as conn:  # this org's own fixture users: out of every window
        await set_owner_tenant(conn, shop.org_id)
        await conn.execute(text("UPDATE users SET created_at = '2026-01-01' WHERE org_id = :o"), {"o": shop.org_id})
    assert (await _tool_values(shop.org_id, shop.admin, "orders_by_status"))["total"] == 0
    assert (await _tool_values(shop.org_id, shop.admin, "revenue"))["revenue"]["amount"] == "0.00"
    assert (await _tool_values(shop.org_id, shop.admin, "most_out_of_stock"))["products"] == []
    assert (await _tool_values(shop.org_id, shop.admin, "new_signups"))["new_signups"] == 0
    assert (await _tool_values(shop.org_id, shop.admin, "ai_usage"))["runs"] == 0
    # Control: the other org's own admin sees its numbers through the same tools.
    assert (await _tool_values(other_shop.org_id, other_shop.admin, "orders_by_status"))["total"] == 6
    # The summary composes the same RLS-scoped queries: nothing of the other org leaks in.
    ctx = ToolContext(shop.org_id, shop.admin, "admin", uuid.uuid4(), uuid.uuid4())
    with clock.freeze(NOW):
        result = await call(ADMIN_TOOLS["activity_summary"], ctx, window="this_month")
    by = {s["metric"]: s["values"] for s in json.loads(result.content_for_model)["sections"]}
    assert by["orders_by_status"]["total"] == 0 and by["revenue"]["revenue"]["amount"] == "0.00"
    assert by["new_signups"]["new_signups"] == 0


async def test_booking_admin_sees_zero_rows_of_another_org(studio: Studio, other_studio: Studio, owner_engine: AsyncEngine) -> None:
    await build_studio(owner_engine, other_studio)
    assert (await _tool_values(studio.org_id, studio.admin, "bookings_by_status"))["total"] == 0
    util = await _tool_values(studio.org_id, studio.admin, "practitioner_utilisation", "this_week")
    assert all(p["booked_minutes"] == 0 for p in util["practitioners"]), util
    assert (await _tool_values(other_studio.org_id, other_studio.admin, "bookings_by_status", "this_week"))["total"] == 8


async def test_rls_alone_scopes_the_readonly_role(shop: Shop, other_shop: Shop, owner_engine: AsyncEngine) -> None:
    """No org filter in the query: RLS is what keeps the other org's rows out."""
    await build_shop(owner_engine, other_shop)
    async with readonly_tx(TenantContext(shop.org_id, shop.admin, "admin")) as conn:
        for table in ("orders", "order_items", "stock_events", "conversations", "messages", "agent_runs", "users"):
            orgs = set(await conn.scalars(text(f"SELECT DISTINCT org_id FROM {table}")))  # noqa: S608
            assert other_shop.org_id not in orgs, table


async def test_a_bad_window_is_an_error_result_not_a_crash(shop: Shop) -> None:
    ctx = ToolContext(shop.org_id, shop.admin, "admin", uuid.uuid4(), uuid.uuid4())
    result = await call(ADMIN_TOOLS["revenue"], ctx, window="custom", start_date="2026-11-05", end_date="2026-11-01")
    assert not result.ok and "end_date is before start_date" in result.content_for_model


# ───────────── admin conversations: DB guard ─────────────


async def test_customer_context_cannot_create_an_admin_conversation(shop: Shop) -> None:
    with pytest.raises(DBAPIError) as ei:
        async with tenant_tx(TenantContext(shop.org_id, shop.x, "customer")) as conn:
            await conn.execute(text("INSERT INTO conversations (org_id, user_id, kind) VALUES (app_org(), app_uid(), 'admin')"))
    assert isinstance(pg_cause(ei), asyncpg.InsufficientPrivilegeError)


async def test_customer_cannot_flip_own_conversation_to_admin(shop: Shop) -> None:
    with pytest.raises(DBAPIError) as ei:
        async with tenant_tx(TenantContext(shop.org_id, shop.x, "customer")) as conn:
            await conn.execute(text("UPDATE conversations SET kind = 'admin' WHERE user_id = app_uid()"))
    assert isinstance(pg_cause(ei), asyncpg.InsufficientPrivilegeError)


async def test_admin_conversations_are_invisible_outside_an_admin_context(shop: Shop, owner_engine: AsyncEngine) -> None:
    async with owner_engine.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        # Even one wrongly owned by a customer (only the owner can write that) stays hidden from them.
        await conn.execute(text("INSERT INTO conversations (org_id, user_id, kind) VALUES (:o, :u, 'admin')"),
                           {"o": shop.org_id, "u": shop.x})  # fmt: skip
    async with tenant_tx(TenantContext(shop.org_id, shop.x, "customer")) as conn:
        assert await conn.scalar(text("SELECT count(*) FROM conversations WHERE kind = 'admin'")) == 0
    async with tenant_tx(TenantContext(shop.org_id, shop.admin, "admin")) as conn:
        assert await conn.scalar(text("SELECT count(*) FROM conversations WHERE kind = 'admin'")) == 1
