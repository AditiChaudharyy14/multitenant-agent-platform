"""Retrieval can never surface another org's knowledge base, even for that org's exact text.

search_kb takes no org parameter: the org comes only from the TenantContext, and RLS on
kb_chunks does the filtering. Run as app_user.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import TenantContext
from app.domain.kb.search import search_kb
from tests.factories import set_owner_tenant

pytestmark = pytest.mark.usefixtures("as_app_user")


async def chunks_of(owner: AsyncEngine, org_id: uuid.UUID) -> dict[uuid.UUID, str]:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, org_id)
        return dict((await conn.execute(text("SELECT id, content FROM kb_chunks ORDER BY id"))).all())


@pytest.mark.parametrize(("searcher", "victim"), [("store", "booking"), ("booking", "store")])
async def test_verbatim_foreign_text_returns_no_foreign_chunks(
    seeded: dict[str, uuid.UUID], owner_engine: AsyncEngine, searcher: str, victim: str
) -> None:
    own = await chunks_of(owner_engine, seeded[searcher])
    foreign = await chunks_of(owner_engine, seeded[victim])
    assert own and foreign and not (own.keys() & foreign.keys())
    ctx = TenantContext(org_id=seeded[searcher], user_id=None, role="customer")
    # Every 7th foreign chunk, so both FAQ and catalog/policy chunks are probed.
    for content in list(foreign.values())[::7]:
        # min_cosine=-1 disables the threshold: even the weakest candidates must be ours.
        res = await search_kb(ctx, content, k=20, min_cosine=-1.0)
        ids = {h.chunk_id for h in res.results}
        assert ids, "search should still return the searcher's own chunks"
        assert ids <= own.keys()
        assert not ids & foreign.keys()


async def test_no_tenant_context_finds_nothing(seeded: dict[str, uuid.UUID], owner_engine: AsyncEngine) -> None:
    """An org id that exists nowhere sees an empty KB (RLS denies, it doesn't error)."""
    ctx = TenantContext(org_id=uuid.uuid4(), user_id=None, role="customer")
    res = await search_kb(ctx, "How long does delivery take?", min_cosine=-1.0)
    assert res.results == [] and res.answerable is False
