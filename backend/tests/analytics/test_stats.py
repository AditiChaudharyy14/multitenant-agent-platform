"""make stats: p50/p95 latency and average tokens per agent, against hand-computed values."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import TenantContext, tenant_tx
from app.domain.analytics.stats import turn_stats
from tests.factories import Shop, set_owner_tenant

pytestmark = pytest.mark.usefixtures("as_app_user")


async def _runs(
    owner: AsyncEngine, shop: Shop, kind: str, user: uuid.UUID, latencies: list[int], tokens: tuple[int, int]
) -> None:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        cid = await conn.scalar(text("INSERT INTO conversations (org_id, user_id, kind) VALUES (:o, :u, :k) RETURNING id"),
                                {"o": shop.org_id, "u": user, "k": kind})  # fmt: skip
        for ms in latencies:
            await conn.execute(text(
                "INSERT INTO agent_runs (org_id, user_id, conversation_id, provider, model, status, latency_ms, "
                "input_tokens, output_tokens) VALUES (:o, :u, :c, 'fake', 'm', 'ok', :ms, :i, :t)"),
                {"o": shop.org_id, "u": user, "c": cid, "ms": ms, "i": tokens[0], "t": tokens[1]})  # fmt: skip


async def test_percentiles_and_averages_per_agent(shop: Shop, other_shop: Shop, owner_engine: AsyncEngine) -> None:
    await _runs(owner_engine, shop, "customer", shop.x, [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000], (1000, 50))
    await _runs(owner_engine, shop, "admin", shop.admin, [2000, 4000], (3000, 100))
    await _runs(owner_engine, other_shop, "customer", other_shop.x, [99_999], (9, 9))  # another org: never counted
    async with tenant_tx(TenantContext(shop.org_id, shop.admin, "admin")) as conn:
        stats = {r["agent"]: r for r in await turn_stats(conn)}
    # percentile_cont over 100..1000: p50 = 550, p95 = 955 (linear interpolation).
    assert stats["customer"] == {"agent": "customer", "turns": 10, "p50_ms": 550, "p95_ms": 955,
                                 "avg_input_tokens": 1000.0, "avg_output_tokens": 50.0}  # fmt: skip
    assert stats["admin"]["p50_ms"] == 3000 and stats["admin"]["p95_ms"] == 3900 and stats["admin"]["turns"] == 2
