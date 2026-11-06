"""Tenant isolation, proven at the database layer while connected as app_user.

Nothing here goes through the API or the LLM: if these pass, a bug higher up still can't
read or write another org's (or another customer's) rows.
"""

import uuid
from datetime import timedelta

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import TenantContext, anon_tx, app_engine, readonly_engine, readonly_tx, tenant_tx
from tests.factories import (
    BASE_TIME,
    CUSTOMER_TABLES,
    TENANT_TABLES,
    Tenant,
    insert_booking,
    pg_cause,
    valid_row_sql,
)

pytestmark = pytest.mark.usefixtures("as_app_user")

RLS_VIOLATION = "row-level security"


# ───────────── 0. Schema guards ─────────────


async def test_every_org_scoped_table_has_forced_rls() -> None:
    """A future table with org_id but no RLS fails here, not in production."""
    async with app_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'org_id' "
                    "AND NOT a.attisdropped "
                    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
                )
            )
        ).all()
    found = {name: (enabled, forced) for name, enabled, forced in rows}
    found.pop("sessions")  # documented exception: looked up by token hash, no RLS
    assert set(found) == set(TENANT_TABLES)
    assert all(flags == (True, True) for flags in found.values()), found


async def test_customer_tables_have_restrictive_own_rows_policy() -> None:
    async with app_engine.connect() as conn:
        tables = (
            await conn.scalars(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = 'public' "
                    "AND policyname = 'own_rows' AND permissive = 'RESTRICTIVE'"
                )
            )
        ).all()
    assert set(tables) == set(CUSTOMER_TABLES)


# ───────────── 1. No settings → nothing ─────────────


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_no_settings_sees_zero_rows(tenants: tuple[Tenant, Tenant], table: str) -> None:
    async with app_engine.begin() as conn:  # deliberately no set_config
        assert await conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_no_settings_cannot_insert(tenants: tuple[Tenant, Tenant], table: str) -> None:
    _, b = tenants
    sql, params = valid_row_sql(table, b)
    with pytest.raises(DBAPIError) as ei:
        async with app_engine.begin() as conn:
            await conn.execute(text(sql), params)
    assert isinstance(pg_cause(ei), asyncpg.InsufficientPrivilegeError)
    assert RLS_VIOLATION in str(pg_cause(ei))


# ───────────── 2. Org A can't see org B ─────────────


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_other_org_rows_invisible(tenants: tuple[Tenant, Tenant], table: str) -> None:
    a, b = tenants
    async with tenant_tx(a.admin_ctx()) as conn:  # admin: widest view inside the org
        orgs = set(await conn.scalars(text(f"SELECT DISTINCT org_id FROM {table}")))
    # Non-vacuous: A's own rows are visible, and only A's.
    assert orgs == {a.org_id}
    assert b.org_id not in orgs


# ───────────── 3. WITH CHECK blocks cross-tenant writes ─────────────


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_insert_for_other_org_rejected(tenants: tuple[Tenant, Tenant], table: str) -> None:
    a, b = tenants
    # A row that would be fully valid in org B; the only thing wrong is the session's org.
    sql, params = valid_row_sql(table, b)
    with pytest.raises(DBAPIError) as ei:
        async with tenant_tx(a.admin_ctx()) as conn:
            await conn.execute(text(sql), params)
    assert isinstance(pg_cause(ei), asyncpg.InsufficientPrivilegeError)
    assert RLS_VIOLATION in str(pg_cause(ei))


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_same_row_accepted_for_own_org(tenants: tuple[Tenant, Tenant], table: str) -> None:
    """Control for the test above: the identical row succeeds in its own org's session."""
    _, b = tenants
    sql, params = valid_row_sql(table, b)
    async with tenant_tx(b.admin_ctx()) as conn:
        await conn.execute(text(sql), params)


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_update_moving_row_to_other_org_rejected(
    tenants: tuple[Tenant, Tenant], table: str
) -> None:
    a, b = tenants
    with pytest.raises(DBAPIError) as ei:
        async with tenant_tx(a.admin_ctx()) as conn:
            await conn.execute(text(f"UPDATE {table} SET org_id = :b"), {"b": b.org_id})
    assert isinstance(pg_cause(ei), asyncpg.InsufficientPrivilegeError)
    assert RLS_VIOLATION in str(pg_cause(ei))


# ───────────── 4. Customers see only their own rows ─────────────


@pytest.mark.parametrize("table", CUSTOMER_TABLES)
async def test_customer_sees_only_own_rows(tenants: tuple[Tenant, Tenant], table: str) -> None:
    _, b = tenants
    async with tenant_tx(b.ctx(b.x, "customer")) as conn:
        owners = set(await conn.scalars(text(f"SELECT DISTINCT user_id FROM {table}")))
    assert owners == {b.x}


@pytest.mark.parametrize("table", CUSTOMER_TABLES)
async def test_admin_sees_all_customers_rows(tenants: tuple[Tenant, Tenant], table: str) -> None:
    _, b = tenants
    async with tenant_tx(b.admin_ctx()) as conn:
        owners = set(await conn.scalars(text(f"SELECT DISTINCT user_id FROM {table}")))
    assert {b.x, b.y} <= owners


@pytest.mark.parametrize("table", CUSTOMER_TABLES)
async def test_customer_lookup_by_id_of_other_customer_finds_nothing(
    tenants: tuple[Tenant, Tenant], table: str
) -> None:
    _, b = tenants
    async with tenant_tx(b.ctx(b.x, "customer")) as conn:
        n = await conn.scalar(text(f"SELECT count(*) FROM {table} WHERE user_id = :y"), {"y": b.y})
    assert n == 0


async def test_customer_cannot_insert_row_owned_by_other_customer(
    tenants: tuple[Tenant, Tenant],
) -> None:
    _, b = tenants
    with pytest.raises(DBAPIError) as ei:
        async with tenant_tx(b.ctx(b.x, "customer")) as conn:
            await conn.execute(
                text(
                    "INSERT INTO orders (org_id, user_id, delivery_address, total) "
                    "VALUES (:o, :y, 'a', 1)"
                ),
                {"o": b.org_id, "y": b.y},
            )
    assert RLS_VIOLATION in str(pg_cause(ei))


async def test_customer_cannot_attach_own_row_to_other_customers_parent(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """X's message in Y's conversation passes RLS (user_id = X) but FK checks bypass RLS,
    so only the owner-consistent composite FK stops it."""
    _, b = tenants
    with pytest.raises(DBAPIError) as ei:
        async with tenant_tx(b.ctx(b.x, "customer")) as conn:
            await conn.execute(
                text(
                    "INSERT INTO messages (org_id, user_id, conversation_id, role) "
                    "VALUES (:o, :x, :c, 'user')"
                ),
                {"o": b.org_id, "x": b.x, "c": b.conversations[b.y]},
            )
    assert isinstance(pg_cause(ei), asyncpg.ForeignKeyViolationError)


async def test_customer_sees_only_self_in_users(tenants: tuple[Tenant, Tenant]) -> None:
    _, b = tenants
    async with tenant_tx(b.ctx(b.x, "customer")) as conn:
        assert set(await conn.scalars(text("SELECT id FROM users"))) == {b.x}
    async with tenant_tx(b.admin_ctx()) as conn:
        assert set(await conn.scalars(text("SELECT id FROM users"))) == {b.admin, b.x, b.y}


async def test_anon_tx_sees_only_its_org_users_for_login(tenants: tuple[Tenant, Tenant]) -> None:
    a, b = tenants
    async with anon_tx(b.org_id) as conn:
        assert set(await conn.scalars(text("SELECT id FROM users"))) == {b.admin, b.x, b.y}
        # No user and no role: customer-owned rows stay invisible.
        assert await conn.scalar(text("SELECT count(*) FROM orders")) == 0
        assert await conn.scalar(text("SELECT current_setting('app.role', true)")) in (None, "")


# ───────────── 5. busy_intervals() ─────────────


async def test_busy_intervals_scoped_to_org_and_confirmed(tenants: tuple[Tenant, Tenant]) -> None:
    a, b = tenants
    cancelled_at = BASE_TIME + timedelta(hours=3)
    async with tenant_tx(b.admin_ctx()) as conn:
        await insert_booking(conn, b, b.x, b.p1, cancelled_at, status="cancelled")

    window = {"f": BASE_TIME - timedelta(days=1), "t": BASE_TIME + timedelta(days=1)}
    async with tenant_tx(b.ctx(b.x, "customer")) as conn:
        result = await conn.execute(text("SELECT * FROM busy_intervals(:f, :t)"), window)
        columns = list(result.keys())
        rows = result.all()

    assert columns == ["practitioner_id", "starts_at", "ends_at"]  # no user_id, no ids
    # Y's booking is included (as busy time only), the cancelled one and org A's are not.
    assert {(r.practitioner_id, r.starts_at) for r in rows} == {
        (b.p1, BASE_TIME),
        (b.p2, BASE_TIME),
    }
    assert not {r.practitioner_id for r in rows} & {a.p1, a.p2}


async def test_busy_intervals_signature_has_no_user_column() -> None:
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT pg_get_function_result(p.oid), p.prosecdef, pg_get_userbyid(p.proowner) "
                    "FROM pg_proc p WHERE p.proname = 'busy_intervals'"
                )
            )
        ).one()
    result_type, security_definer, owner = row
    assert "user_id" not in result_type
    assert security_definer is True
    assert owner == "rls_definer"


async def test_busy_intervals_empty_without_org(tenants: tuple[Tenant, Tenant]) -> None:
    async with app_engine.begin() as conn:  # no settings
        n = await conn.scalar(
            text("SELECT count(*) FROM busy_intervals(:f, :t)"),
            {"f": BASE_TIME - timedelta(days=365), "t": BASE_TIME + timedelta(days=365)},
        )
    assert n == 0


async def test_readonly_role_cannot_call_busy_intervals(tenants: tuple[Tenant, Tenant]) -> None:
    _, b = tenants
    with pytest.raises(DBAPIError) as ei:
        async with readonly_tx(b.admin_ctx()) as conn:
            await conn.execute(text("SELECT * FROM busy_intervals(now(), now())"))
    assert isinstance(pg_cause(ei), asyncpg.InsufficientPrivilegeError)


# ───────────── 6. Exclusion constraints ─────────────

SLOT = BASE_TIME + timedelta(days=10)


async def test_practitioner_overlap_rejected(tenants: tuple[Tenant, Tenant]) -> None:
    _, b = tenants
    async with tenant_tx(b.ctx(b.x, "customer")) as conn:
        await insert_booking(conn, b, b.x, b.p1, SLOT)
    # Y can't see X's booking (RLS), yet the database still refuses the overlap.
    with pytest.raises(DBAPIError) as ei:
        async with tenant_tx(b.ctx(b.y, "customer")) as conn:
            await insert_booking(conn, b, b.y, b.p1, SLOT + timedelta(minutes=30))
    err = pg_cause(ei)
    assert isinstance(err, asyncpg.ExclusionViolationError)
    assert err.constraint_name == "no_practitioner_overlap"


async def test_back_to_back_bookings_allowed(tenants: tuple[Tenant, Tenant]) -> None:
    _, b = tenants
    async with tenant_tx(b.admin_ctx()) as conn:
        await insert_booking(conn, b, b.x, b.p1, SLOT)  # [10:00, 11:00)
        await insert_booking(conn, b, b.y, b.p1, SLOT + timedelta(hours=1))  # [11:00, 12:00)


async def test_cancelled_booking_does_not_block(tenants: tuple[Tenant, Tenant]) -> None:
    _, b = tenants
    async with tenant_tx(b.admin_ctx()) as conn:
        await insert_booking(conn, b, b.x, b.p1, SLOT, status="cancelled")
        await insert_booking(conn, b, b.y, b.p1, SLOT)


async def test_customer_overlap_across_practitioners_rejected(
    tenants: tuple[Tenant, Tenant],
) -> None:
    _, b = tenants
    async with tenant_tx(b.ctx(b.x, "customer")) as conn:
        await insert_booking(conn, b, b.x, b.p1, SLOT)
    with pytest.raises(DBAPIError) as ei:
        async with tenant_tx(b.ctx(b.x, "customer")) as conn:
            await insert_booking(conn, b, b.x, b.p2, SLOT + timedelta(minutes=15))
    err = pg_cause(ei)
    assert isinstance(err, asyncpg.ExclusionViolationError)
    assert err.constraint_name == "no_customer_overlap"


async def test_customer_back_to_back_across_practitioners_allowed(
    tenants: tuple[Tenant, Tenant],
) -> None:
    _, b = tenants
    async with tenant_tx(b.ctx(b.x, "customer")) as conn:
        await insert_booking(conn, b, b.x, b.p1, SLOT)
        await insert_booking(conn, b, b.x, b.p2, SLOT + timedelta(hours=1))


# ───────────── 7. app_readonly writes nothing ─────────────

ALL_TABLES = (*TENANT_TABLES, "orgs", "sessions")
WRITES = {
    "insert": "INSERT INTO {t} DEFAULT VALUES",
    "update": "UPDATE {t} SET {col} = {col}",
    "delete": "DELETE FROM {t}",
}


def _write_sql(op: str, table: str) -> str:
    return WRITES[op].format(t=table, col="id" if table == "orgs" else "org_id")


@pytest.mark.parametrize("op", WRITES)
@pytest.mark.parametrize("table", ALL_TABLES)
async def test_readonly_tx_rejects_writes(
    tenants: tuple[Tenant, Tenant], table: str, op: str
) -> None:
    _, b = tenants
    with pytest.raises(DBAPIError) as ei:
        async with readonly_tx(b.admin_ctx()) as conn:
            await conn.execute(text(_write_sql(op, table)))
    assert isinstance(pg_cause(ei), asyncpg.ReadOnlySQLTransactionError)


@pytest.mark.parametrize("op", WRITES)
@pytest.mark.parametrize("table", ALL_TABLES)
async def test_readonly_role_has_no_write_grants(
    tenants: tuple[Tenant, Tenant], table: str, op: str
) -> None:
    """Even without READ ONLY, the role itself holds no write privilege."""
    with pytest.raises(DBAPIError) as ei:
        async with readonly_engine.begin() as conn:
            await conn.execute(text(_write_sql(op, table)))
    assert isinstance(pg_cause(ei), asyncpg.InsufficientPrivilegeError)


async def test_readonly_tx_is_read_only_and_scoped(tenants: tuple[Tenant, Tenant]) -> None:
    a, b = tenants
    async with readonly_tx(b.admin_ctx()) as conn:
        assert await conn.scalar(text("SELECT current_user")) == "app_readonly"
        assert await conn.scalar(text("SHOW transaction_read_only")) == "on"
        orgs = set(await conn.scalars(text("SELECT DISTINCT org_id FROM orders")))
    assert orgs == {b.org_id}


# ───────────── Settings never leak through the pool ─────────────


async def test_tenant_settings_do_not_leak_to_next_checkout(
    tenants: tuple[Tenant, Tenant],
) -> None:
    a, _ = tenants
    async with tenant_tx(a.admin_ctx()) as conn:
        first_pid = await conn.scalar(text("SELECT pg_backend_pid()"))
        assert await conn.scalar(text("SELECT current_setting('app.org_id', true)")) == str(a.org_id)

    # The pool holds exactly that one connection, so this checkout reuses it.
    async with app_engine.begin() as conn:
        second_pid = await conn.scalar(text("SELECT pg_backend_pid()"))
        org = await conn.scalar(text("SELECT current_setting('app.org_id', true)"))
        user = await conn.scalar(text("SELECT current_setting('app.user_id', true)"))
        role = await conn.scalar(text("SELECT current_setting('app.role', true)"))
        visible = await conn.scalar(text("SELECT count(*) FROM orders"))

    assert second_pid == first_pid, "test must reuse the same pooled connection"
    assert org in (None, "") and user in (None, "") and role in (None, "")
    assert visible == 0


async def test_control_plain_set_would_leak() -> None:
    """Proves the leak test above can fail: session-level SET survives the checkout,
    which is exactly why tenant_tx uses set_config(..., true)."""
    leaked = str(uuid.uuid4())
    async with app_engine.connect() as conn:
        first_pid = await conn.scalar(text("SELECT pg_backend_pid()"))
        await conn.execute(text(f"SET app.org_id = '{leaked}'"))
        await conn.commit()
    try:
        async with app_engine.begin() as conn:
            assert await conn.scalar(text("SELECT pg_backend_pid()")) == first_pid
            assert await conn.scalar(text("SELECT current_setting('app.org_id', true)")) == leaked
    finally:
        await app_engine.dispose()  # throw away the polluted connection


def test_tenant_context_rejects_unknown_role() -> None:
    with pytest.raises(ValueError):
        TenantContext(org_id=uuid.uuid4(), user_id=None, role="superuser")
