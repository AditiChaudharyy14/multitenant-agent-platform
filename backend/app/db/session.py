"""Database engines and per-request transactions.

One request is one transaction. Its first statement stores the caller's identity in
transaction-local settings (set_config(..., true)), which the RLS policies read through
app_org(), app_uid() and app_role(). Being transaction-local, the settings vanish at
COMMIT/ROLLBACK, so a pooled connection can never carry one tenant's identity into the
next request. Plain SET is never used: it is session-level and would leak exactly that way.

Creating an engine doesn't connect; connections open on first use.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.config import get_settings

ROLES = frozenset({"customer", "admin"})

# Runtime engine: app_user, not an owner, NOBYPASSRLS.
app_engine = create_async_engine(get_settings().database_url_app, pool_pre_ping=True)
# Admin analytics: app_readonly, SELECT-only grants, and every transaction is READ ONLY.
readonly_engine = create_async_engine(get_settings().database_url_readonly, pool_pre_ping=True)

# Bind parameters work here because this is a SELECT; asyncpg rejects them in SET.
_SET_TENANT = text(
    "SELECT set_config('app.org_id', :org_id, true),"
    " set_config('app.user_id', :user_id, true),"
    " set_config('app.role', :role, true)"
)
_SET_ORG_ONLY = text("SELECT set_config('app.org_id', :org_id, true)")


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Who is acting. Always built from the server-side session, never from the request."""

    org_id: UUID
    user_id: UUID | None
    role: str  # 'customer' | 'admin'

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}")


def _tenant_params(ctx: TenantContext) -> dict[str, str]:
    # '' rather than NULL: set_config needs text, and the policies' NULLIF turns '' into NULL.
    return {
        "org_id": str(ctx.org_id),
        "user_id": str(ctx.user_id) if ctx.user_id else "",
        "role": ctx.role,
    }


@asynccontextmanager
async def tenant_tx(ctx: TenantContext) -> AsyncIterator[AsyncConnection]:
    """One transaction as app_user, scoped to ctx. Commits on success, rolls back on error."""
    async with app_engine.begin() as conn:
        await conn.execute(_SET_TENANT, _tenant_params(ctx))
        yield conn


@asynccontextmanager
async def readonly_tx(ctx: TenantContext) -> AsyncIterator[AsyncConnection]:
    """One READ ONLY transaction as app_readonly, scoped to ctx (admin analytics)."""
    async with readonly_engine.begin() as conn:
        # Must be the transaction's first statement, before any query runs.
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        await conn.execute(_SET_TENANT, _tenant_params(ctx))
        yield conn


@asynccontextmanager
async def anon_tx(org_id: UUID) -> AsyncIterator[AsyncConnection]:
    """For login and signup, before anyone is authenticated: only the org is known.

    With no user and role 'none', customer-owned rows stay invisible; users of this org
    are visible so a login can look up the email.
    """
    async with app_engine.begin() as conn:
        await conn.execute(_SET_ORG_ONLY, {"org_id": str(org_id)})
        yield conn
