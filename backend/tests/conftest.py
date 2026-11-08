"""Shared test setup.

Every test runs against the separate `app_test` database on the throwaway db-ephemeral
Postgres (tmpfs, durability off; `make test` starts it), never the dev `db` service. The URLs are rewritten here, before anything from `app` is
imported, because app.config caches settings and app.db.session builds its engines
at import time.
"""

import os

from sqlalchemy.engine import make_url

TEST_DATABASE = "app_test"
TEST_DB_HOST = os.environ.get("TEST_DB_HOST", "db-ephemeral")
_URL_VARS = ("DATABASE_URL_OWNER", "DATABASE_URL_APP", "DATABASE_URL_READONLY")

for _var in _URL_VARS:
    # Same host, role and password as the container's URL; only the database differs.
    os.environ[_var] = make_url(os.environ[_var]).set(database=TEST_DATABASE).render_as_string(hide_password=False)

# No test ever talks to a real LLM: `make test` must be offline, deterministic and free.
# The ops container has no API keys anyway; this makes the guarantee explicit.
os.environ["LLM_PROVIDER"] = "fake"
os.environ["GEMINI_API_KEY"] = ""
os.environ["MISTRAL_API_KEY"] = ""
os.environ["EVAL_MODE"] = "off"

# The app imports must come after the rewrite above.
import uuid  # noqa: E402
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
from tests.factories import Shop, Studio, Tenant, seed_shop, seed_studio, seed_tenant  # noqa: E402

# Refuse to run at all if any engine could reach a database other than app_test.
for _url in (
    require_owner_url(),
    get_settings().database_url_app,
    get_settings().database_url_readonly,
):
    # Only the database name and host are compared: pytest prints an assert's operands, and
    # the URL itself holds a password.
    _database, _host = make_url(_url).database, make_url(_url).host
    assert _database == TEST_DATABASE, "tests must only touch app_test"
    # Structural, not by convention: the dev `db` service is never reachable from tests.
    assert _host == TEST_DB_HOST, f"tests must run against {TEST_DB_HOST} (use `make test`)"
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
async def shop(owner_engine: AsyncEngine) -> Shop:
    """A fresh store org with known products and stock (tests/factories.py SHOP_PRODUCTS)."""
    return await seed_shop(owner_engine)


@pytest.fixture
async def other_shop(owner_engine: AsyncEngine) -> Shop:
    """A second, unrelated store org, for cross-org isolation tests."""
    return await seed_shop(owner_engine)


@pytest.fixture
async def studio(owner_engine: AsyncEngine) -> Studio:
    """A fresh booking org with known hours, holiday, services and practitioners
    (tests/factories.py STUDIO_*)."""
    return await seed_studio(owner_engine)


@pytest.fixture
async def other_studio(owner_engine: AsyncEngine) -> Studio:
    """A second, unrelated booking org, for cross-org isolation tests."""
    return await seed_studio(owner_engine)


@pytest.fixture
async def as_app_user() -> None:
    """Guard for isolation tests: the runtime engine really is app_user on app_test.

    RLS is bypassed by superusers and (without FORCE) by owners, so a test that passed as
    any other role would prove nothing.
    """
    async with app_engine.connect() as conn:
        row = (await conn.execute(text("SELECT current_user, current_database()"))).one()
        flags = (
            await conn.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
        ).one()
        owned = await conn.scalar(text("SELECT count(*) FROM pg_tables WHERE tableowner = current_user"))
    assert tuple(row) == ("app_user", TEST_DATABASE)
    assert tuple(flags) == (False, False), "app_user must be NOSUPERUSER NOBYPASSRLS"
    assert owned == 0, "app_user must own no tables"


def first_per_vertical(items: list, vertical_of=lambda x: x.vertical, slug_of=lambda x: x.slug) -> dict:
    """vertical → the org with the alphabetically first slug of that vertical.

    seed/ may hold several orgs of one vertical (adding one takes config + seed only).
    Tests that assert on one org's demo rows use this pick, so they stay deterministic
    without naming a slug; tests/isolation/test_all_seed_orgs.py covers every org.
    """
    picked: dict = {}
    for item in sorted(items, key=slug_of):
        picked.setdefault(vertical_of(item), item)
    return picked


@pytest.fixture
async def org_configs() -> list[OrgConfig]:
    """Every org in seed/, in slug order."""
    return load_org_configs(get_settings().seed_dir)


@pytest.fixture
async def demo_org_configs(org_configs: list[OrgConfig]) -> list[OrgConfig]:
    """One org per vertical (first_per_vertical), for tests that copy a vertical's config."""
    return list(first_per_vertical(org_configs).values())


@pytest.fixture
async def synced_orgs(org_configs: list[OrgConfig]) -> dict[str, OrgConfig]:
    """All seed orgs upserted into app_test; one per vertical returned, so tests never name a slug."""
    await sync_orgs(require_owner_url(), org_configs)
    return first_per_vertical(org_configs)


# Fixed seed "now" for tests: Monday 9 Nov 2026, 10:00 in the orgs' timezone. Two days
# later is a seeded holiday, so the booking roll-forward is exercised deterministically.
SEED_NOW = "2026-11-09T10:00"


@pytest.fixture(scope="session")
def seeded_all(migrated_test_db: None) -> dict[str, uuid.UUID]:
    """Run the real `make seed` code once against app_test; org ids of EVERY seed org, by slug.

    Base data.yaml only (analytics=False), like the eval template: the tests below assert
    on exactly the demo rows. tests/domain/test_seed_analytics.py covers analytics.yaml.

    Sync with asyncio.run, like migrated_test_db: a session fixture can't share the
    per-test event loops. The seeder uses its own NullPool owner engine.
    """
    import asyncio

    from app.seed.run import seed_all

    result = asyncio.run(seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW, analytics=False))
    return {r.slug: r.org_id for r in result}


@pytest.fixture(scope="session")
def seeded(seeded_all: dict[str, uuid.UUID]) -> dict[str, uuid.UUID]:
    """Seeded org ids keyed by vertical (first_per_vertical's pick)."""
    picked = first_per_vertical(list(seeded_all), vertical_of=_vertical_of_slug, slug_of=lambda s: s)
    return {v: seeded_all[slug] for v, slug in picked.items()}


def _vertical_of_slug(slug: str) -> str:
    return next(c.vertical for c in load_org_configs(get_settings().seed_dir) if c.slug == slug)
