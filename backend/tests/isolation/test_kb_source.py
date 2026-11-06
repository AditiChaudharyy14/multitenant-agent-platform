"""GET /api/kb/source: the citation drawer's chunk lookup, as app_user over the real app.

The ref names a chunk (doc_id#section) but never an org. RLS on kb_chunks decides what is
visible, so another org's real ref is indistinguishable from one that never existed.
Both orgs here are fresh copies of the seed configs, each with its own real KB, so the
seeded orgs (and the seed idempotency test) are never touched.
"""

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth.ratelimit import login_limiter
from app.config import get_settings, require_owner_url
from app.domain.kb.ingest import insert_chunks, prepare_kb
from app.main import app
from app.seed.data import load_seed_data
from app.tenancy.orgs import OrgConfig, sync_orgs
from tests.factories import set_owner_tenant, unique_prefix

pytestmark = pytest.mark.usefixtures("as_app_user")

ORIGIN = "http://localhost:3000"
NOT_FOUND = {"detail": "Not found"}
# Real chunks from each vertical's seed KB. Both orgs have a doc called `faq`, so the
# foreign ref differs from an own one only in its section.
STORE_REF = "faq#how-long-does-delivery-take"
BOOKING_REF = "faq#do-i-need-to-book-in-advance"


@pytest.fixture
async def kb_orgs(org_configs: list[OrgConfig], owner_engine: AsyncEngine) -> dict[str, str]:
    """vertical → slug of a fresh org holding that vertical's real seed KB."""
    copies = {c.vertical: c.model_copy(update={"slug": f"kbsrc-{c.vertical}-{uuid.uuid4().hex[:10]}",
                                               "order_prefix": unique_prefix() if c.order_prefix else None,
                                               "booking_prefix": unique_prefix() if c.booking_prefix else None})
              for c in org_configs}  # fmt: skip
    await sync_orgs(require_owner_url(), list(copies.values()))
    for original in org_configs:
        org_dir = Path(get_settings().seed_dir) / original.slug
        data = load_seed_data(org_dir / "data.yaml", original.vertical)
        catalogs = {"products": {p.name: p.sku for p in data.products},
                    "services": {s.name: None for s in data.services}}  # fmt: skip
        kb = await prepare_kb(org_dir / "kb", catalogs)
        async with owner_engine.begin() as conn:
            org_id = await conn.scalar(text("SELECT id FROM orgs WHERE slug = :s"),
                                       {"s": copies[original.vertical].slug})  # fmt: skip
            await set_owner_tenant(conn, org_id)
            await insert_chunks(conn, org_id, kb)
    return {v: c.slug for v, c in copies.items()}


def client(sid: str | None = None) -> httpx.AsyncClient:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                          headers={"Origin": ORIGIN}, timeout=30)  # fmt: skip
    if sid:
        c.cookies.set("sid", sid)
    return c


async def signup(slug: str) -> str:
    login_limiter.reset()
    async with client() as c:
        r = await c.post(f"/api/orgs/{slug}/auth/signup",
                         json={"email": f"kb-{uuid.uuid4().hex[:12]}@example.test",
                               "password": "correct horse battery", "display_name": "Asha"})  # fmt: skip
    assert r.status_code == 201, r.text
    return r.cookies["sid"]


async def get_source(sid: str | None, ref: str) -> httpx.Response:
    async with client(sid) as c:
        # params= URL-encodes the '#', which would otherwise start a fragment.
        return await c.get("/api/kb/source", params={"ref": ref})


async def test_own_ref_returns_the_chunk(kb_orgs: dict[str, str]) -> None:
    sid = await signup(kb_orgs["store"])
    r = await get_source(sid, STORE_REF)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ref"] == STORE_REF
    assert body["doc_id"] == "faq" and body["section"] == "how-long-does-delivery-take"
    assert body["doc_title"] and "deliver" in body["content"].lower()
    assert set(body) == {"ref", "doc_id", "doc_title", "section", "content"}  # no ids, no org


@pytest.mark.parametrize(("viewer", "foreign_ref", "own_ref"),
                         [("store", BOOKING_REF, STORE_REF), ("booking", STORE_REF, BOOKING_REF)])  # fmt: skip
async def test_other_orgs_real_ref_is_404(kb_orgs: dict[str, str], viewer: str, foreign_ref: str, own_ref: str) -> None:
    sid = await signup(kb_orgs[viewer])
    assert (await get_source(sid, own_ref)).status_code == 200  # the viewer's own KB is there
    r = await get_source(sid, foreign_ref)
    assert (r.status_code, r.json()) == (404, NOT_FOUND)


@pytest.mark.parametrize("ref", ["faq#no-such-section", "nosuchdoc#x", "no-hash", "#", "faq#", "#section", "a" * 400])
async def test_missing_or_malformed_ref_has_the_same_404(kb_orgs: dict[str, str], ref: str) -> None:
    sid = await signup(kb_orgs["store"])
    r = await get_source(sid, ref)
    assert (r.status_code, r.json()) == (404, NOT_FOUND)


async def test_without_a_session_is_401(kb_orgs: dict[str, str]) -> None:
    r = await get_source(None, STORE_REF)
    assert r.status_code == 401
