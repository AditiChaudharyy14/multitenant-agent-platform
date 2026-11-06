"""Shared test setup.

Every test runs against the separate `app_test` database (created by `make testdb`),
never the dev database. The URLs are rewritten here, before anything from `app` is
imported, because app.config caches settings and app.db.session builds its engines
at import time.
"""

import os

from sqlalchemy.engine import make_url

TEST_DATABASE = "app_test"
_URL_VARS = ("DATABASE_URL_OWNER", "DATABASE_URL_APP", "DATABASE_URL_READONLY")

for _var in _URL_VARS:
    # Same host, role and password as the container's URL; only the database differs.
    os.environ[_var] = (
        make_url(os.environ[_var]).set(database=TEST_DATABASE).render_as_string(hide_password=False)
    )

# The app imports must come after the rewrite above.
from collections.abc import AsyncIterator, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import pool, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from app.config import get_settings, require_owner_url  # noqa: E402
from app.db.session import app_engine, readonly_engine  # noqa: E402
from app.tenancy.orgs import OrgConfig, load_org_configs, sync_orgs  # noqa: E402
from tests.factories import Tenant, seed_tenant  # noqa: E402

# Refuse to run at all if any engine could reach a database other than app_test.
for _url in (
    require_owner_url(),
    get_settings().database_url_app,
    get_settings().database_url_readonly,
):
    assert make_url(_url).database == TEST_DATABASE, "tests must only touch app_test"
assert app_engine.url.database == TEST_DATABASE
assert readonly_engine.url.database == TEST_DATABASE

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def migrated_test_db() -> Iterator[None]:
    """Rebuild app_test's schema from scratch once per run, which also exercises downgrade().

    Sync on purpose: migrations/env.py calls asyncio.run(), which can't run inside a loop.
    """
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
async def dispose_engines() -> AsyncIterator[None]:
    """Close pooled connections after each test, as each test gets its own event loop."""
    yield
    await app_engine.dispose()
    await readonly_engine.dispose()


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    """app_owner engine, for fixture setup only. Runtime code never connects as the owner."""
    engine = create_async_engine(require_owner_url(), poolclass=pool.NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def tenants(owner_engine: AsyncEngine) -> tuple[Tenant, Tenant]:
    """Two fresh orgs, each with rows in every tenant table. Org A is store, org B is booking.

    Each test gets new orgs with unique slugs, so tests can't see each other's data, and the
    whole schema is rebuilt on the next run, so no teardown is needed.
    """
    a = await seed_tenant(owner_engine, vertical="store")
    b = await seed_tenant(owner_engine, vertical="booking")
    return a, b


@pytest.fixture
async def as_app_user() -> None:
    """Guard for isolation tests: the runtime engine really is app_user on app_test.

    RLS is bypassed by superusers and (without FORCE) by owners, so a test that passed as
    any other role would prove nothing.
    """
    async with app_engine.connect() as conn:
        row = (await conn.execute(text("SELECT current_user, current_database()"))).one()
        flags = (
            await conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        owned = await conn.scalar(
            text("SELECT count(*) FROM pg_tables WHERE tableowner = current_user")
        )
    assert tuple(row) == ("app_user", TEST_DATABASE)
    assert tuple(flags) == (False, False), "app_user must be NOSUPERUSER NOBYPASSRLS"
    assert owned == 0, "app_user must own no tables"


@pytest.fixture
async def org_configs() -> list[OrgConfig]:
    return load_org_configs(get_settings().seed_dir)


@pytest.fixture
async def synced_orgs(org_configs: list[OrgConfig]) -> dict[str, OrgConfig]:
    """The seed orgs upserted into app_test, keyed by vertical so tests never name a slug."""
    await sync_orgs(require_owner_url(), org_configs)
    return {c.vertical: c for c in org_configs}
