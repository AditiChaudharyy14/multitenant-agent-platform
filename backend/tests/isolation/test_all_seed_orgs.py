"""Isolation over EVERY org in seed/, parametrised at collection time.

Adding an org is config + seed data only (invariant 11), so these tests pick it up with no
edit: drop a folder into seed/ and each check below runs once more for it. They use the
real seeded rows (`make seed`'s own code), connected as app_user.
"""

import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.tools.registry import REGISTRY
from app.auth.ratelimit import login_limiter
from app.config import get_settings
from app.db.session import TenantContext, tenant_tx
from app.domain.kb.search import search_kb
from app.main import app
from app.tenancy.orgs import load_org_configs
from tests.factories import TENANT_TABLES, set_owner_tenant

pytestmark = pytest.mark.usefixtures("as_app_user")

SEED = Path(get_settings().seed_dir)
CONFIGS = load_org_configs(SEED)
SLUGS = [c.slug for c in CONFIGS]
ORIGIN = "http://localhost:3000"


def _users(slug: str) -> list[dict]:
    return yaml.safe_load((SEED / slug / "data.yaml").read_text(encoding="utf-8"))["users"]


async def _user_id(owner: AsyncEngine, org_id: uuid.UUID, role: str) -> uuid.UUID:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, org_id)
        uid = await conn.scalar(text("SELECT id FROM users WHERE role = :r ORDER BY email LIMIT 1"), {"r": role})
    assert uid is not None, f"seed needs at least one {role}"
    return uid


def test_org_slugs_and_prefixes_are_unique() -> None:
    assert len(SLUGS) >= 2
    assert len(set(SLUGS)) == len(SLUGS)
    prefixes = [p for c in CONFIGS for p in (c.order_prefix, c.booking_prefix) if p]
    assert len(set(prefixes)) == len(prefixes)


@pytest.mark.parametrize("slug", SLUGS)
def test_every_org_has_a_known_vertical(slug: str) -> None:
    cfg = next(c for c in CONFIGS if c.slug == slug)
    assert cfg.vertical in REGISTRY  # behaviour comes from the vertical, so it must exist


@pytest.mark.parametrize("role", ["customer", "admin"])
@pytest.mark.parametrize("slug", SLUGS)
async def test_org_sees_only_its_own_rows_in_every_tenant_table(
    seeded_all: dict[str, uuid.UUID], owner_engine: AsyncEngine, slug: str, role: str
) -> None:
    org_id = seeded_all[slug]
    uid = await _user_id(owner_engine, org_id, role)
    async with tenant_tx(TenantContext(org_id, uid, role)) as conn:
        for table in TENANT_TABLES:
            foreign = await conn.scalar(text(f"SELECT count(*) FROM {table} WHERE org_id <> :o"), {"o": org_id})  # noqa: S608
            assert foreign == 0, (slug, role, table)
        assert await conn.scalar(text("SELECT count(*) FROM users")) > 0  # control: it does see its own


@pytest.mark.parametrize("slug", SLUGS)
async def test_kb_search_never_returns_another_orgs_chunks(
    seeded_all: dict[str, uuid.UUID], owner_engine: AsyncEngine, slug: str
) -> None:
    org_id = seeded_all[slug]
    chunks: dict[uuid.UUID, dict[uuid.UUID, str]] = {}
    for oid in seeded_all.values():  # FORCE RLS binds the owner too: read each org in its own scope
        async with owner_engine.begin() as conn:
            await set_owner_tenant(conn, oid)
            chunks[oid] = dict((await conn.execute(text("SELECT id, content FROM kb_chunks ORDER BY id"))).all())
    own = chunks[org_id].keys()
    foreign = [content for oid, cs in chunks.items() if oid != org_id for content in cs.values()]
    assert own and foreign
    ctx = TenantContext(org_id=org_id, user_id=None, role="customer")
    for content in foreign[::9]:  # verbatim foreign text, threshold off: still only our own chunks
        ids = {h.chunk_id for h in (await search_kb(ctx, content, k=20, min_cosine=-1.0)).results}
        assert ids and ids <= own, slug


@pytest.mark.parametrize("slug", SLUGS)
async def test_a_customer_can_log_in_only_at_their_own_org(seeded_all: dict[str, uuid.UUID], slug: str) -> None:
    login_limiter.reset()
    user = next(u for u in _users(slug) if u["role"] == "customer")
    body = {"email": user["email"], "password": user["password"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                 headers={"Origin": ORIGIN}) as c:  # fmt: skip
        own = await c.post(f"/api/orgs/{slug}/auth/login", json=body)
        assert own.status_code == 200, own.text
        c.cookies.set("sid", own.cookies["sid"])
        me = await c.get("/api/me")
        assert me.status_code == 200 and me.json()["org"]["slug"] == slug
        for other in (s for s in SLUGS if s != slug):
            r = await c.post(f"/api/orgs/{other}/auth/login", json=body)
            assert r.status_code == 401 and "sid" not in r.cookies, (slug, other)
