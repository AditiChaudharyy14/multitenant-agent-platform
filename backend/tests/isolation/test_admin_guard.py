"""DB-level guard: an admin user can only be created or promoted by an admin context
(or app_owner). Even a signup bug that sends role='admin' can't make an admin."""

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.session import anon_tx, tenant_tx
from tests.factories import Tenant, new_id, pg_cause

pytestmark = pytest.mark.usefixtures("as_app_user")

INSERT_USER = text(
    "INSERT INTO users (org_id, email, display_name, password_hash, role) "
    "VALUES (:o, :e, 'n', 'x', :r)"
)


async def _insert(conn: AsyncConnection, t: Tenant, role: str) -> None:
    await conn.execute(INSERT_USER, {"o": t.org_id, "e": f"{new_id().hex}@x.test", "r": role})


async def test_anon_tx_cannot_insert_admin(tenants: tuple[Tenant, Tenant]) -> None:
    a, _ = tenants
    with pytest.raises(DBAPIError) as ei:
        async with anon_tx(a.org_id) as conn:
            await _insert(conn, a, "admin")
    assert isinstance(pg_cause(ei), asyncpg.InsufficientPrivilegeError)


async def test_anon_tx_can_insert_customer(tenants: tuple[Tenant, Tenant]) -> None:
    a, _ = tenants
    async with anon_tx(a.org_id) as conn:
        await _insert(conn, a, "customer")


async def test_customer_cannot_insert_admin(tenants: tuple[Tenant, Tenant]) -> None:
    a, _ = tenants
    with pytest.raises(DBAPIError) as ei:
        async with tenant_tx(a.ctx(a.x, "customer")) as conn:
            await _insert(conn, a, "admin")
    # Rejected either by the guard or by own_user_row (id != self); both are RLS-level.
    assert isinstance(pg_cause(ei), asyncpg.InsufficientPrivilegeError)


async def test_customer_cannot_promote_self(tenants: tuple[Tenant, Tenant]) -> None:
    """own_user_row lets a customer update their own row; the guard stops role changes."""
    a, _ = tenants
    with pytest.raises(DBAPIError) as ei:
        async with tenant_tx(a.ctx(a.x, "customer")) as conn:
            await conn.execute(text("UPDATE users SET role = 'admin' WHERE id = :x"), {"x": a.x})
    assert isinstance(pg_cause(ei), asyncpg.InsufficientPrivilegeError)


async def test_anon_tx_cannot_promote(tenants: tuple[Tenant, Tenant]) -> None:
    a, _ = tenants
    with pytest.raises(DBAPIError) as ei:
        async with anon_tx(a.org_id) as conn:
            await conn.execute(text("UPDATE users SET role = 'admin' WHERE id = :x"), {"x": a.x})
    assert isinstance(pg_cause(ei), asyncpg.InsufficientPrivilegeError)


async def test_admin_ctx_can_insert_admin(tenants: tuple[Tenant, Tenant]) -> None:
    a, _ = tenants
    async with tenant_tx(a.admin_ctx()) as conn:
        await _insert(conn, a, "admin")


async def test_anon_tx_can_rehash_existing_admin(tenants: tuple[Tenant, Tenant]) -> None:
    """Login rehashes in anon_tx; that must still work for admins (role unchanged)."""
    a, _ = tenants
    async with anon_tx(a.org_id) as conn:
        n = await conn.execute(
            text("UPDATE users SET password_hash = 'y' WHERE id = :id"), {"id": a.admin}
        )
    assert n.rowcount == 1


async def test_owner_can_insert_admin_without_app_role(
    tenants: tuple[Tenant, Tenant], owner_engine: AsyncEngine
) -> None:
    a, _ = tenants
    async with owner_engine.begin() as conn:
        # Only the org is set (FORCE RLS still applies to the owner); app.role is unset.
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(a.org_id)})
        await _insert(conn, a, "admin")
