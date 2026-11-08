"""`make stats`: p50/p95 turn latency and average tokens per turn, per org and agent.

Runs as app_user in an admin-scoped transaction for each org (like `make advance`): no
owner credentials, and RLS still applies.
"""

import asyncio
import sys

from sqlalchemy import text

from app.db.session import TenantContext, app_engine, tenant_tx
from app.domain.analytics.stats import turn_stats


async def run() -> int:
    async with app_engine.connect() as conn:  # orgs is global: app_user may read it
        orgs = (await conn.execute(text("SELECT id, slug FROM orgs ORDER BY slug"))).all()
    print(f"{'org':18} {'agent':9} {'turns':>6} {'p50 ms':>8} {'p95 ms':>8} {'avg in':>8} {'avg out':>8}")
    try:
        for org_id, slug in orgs:
            async with tenant_tx(TenantContext(org_id=org_id, user_id=None, role="admin")) as conn:
                for r in await turn_stats(conn):
                    print(f"{slug:18} {r['agent']:9} {r['turns']:>6} {r['p50_ms'] or '-':>8} {r['p95_ms'] or '-':>8} "
                          f"{r['avg_input_tokens']:>8} {r['avg_output_tokens']:>8}")  # fmt: skip
    finally:
        await app_engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
