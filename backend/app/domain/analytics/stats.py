"""Per-agent turn statistics from agent_runs: p50/p95 latency and average tokens per turn.

Read-only and org-scoped like the admin metrics: it runs on the caller's tenant connection
(RLS limits it to that org) and filters on org_id = app_org() as well. Used by `make stats`.
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def turn_stats(conn: AsyncConnection) -> list[dict[str, Any]]:
    """One row per agent (customer, admin) with turns, p50/p95 latency (ms) and average
    input/output tokens per turn. Runs with no recorded latency are left out of the
    percentiles but counted."""
    rows = (await conn.execute(text(
        "SELECT c.kind AS agent, count(*) AS turns, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY r.latency_ms) AS p50_ms, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY r.latency_ms) AS p95_ms, "
        "avg(r.input_tokens) AS avg_in, avg(r.output_tokens) AS avg_out "
        "FROM agent_runs r JOIN conversations c ON c.id = r.conversation_id "
        "WHERE r.org_id = app_org() GROUP BY c.kind ORDER BY c.kind"))).all()  # fmt: skip
    return [
        {"agent": r.agent, "turns": r.turns,
         "p50_ms": round(r.p50_ms) if r.p50_ms is not None else None,
         "p95_ms": round(r.p95_ms) if r.p95_ms is not None else None,
         "avg_input_tokens": round(float(r.avg_in), 1), "avg_output_tokens": round(float(r.avg_out), 1)}
        for r in rows
    ]  # fmt: skip
