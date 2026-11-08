"""Admin analytics tools: one read-only tool per metric in app/domain/analytics/metrics.py.

Every handler runs its fixed query in readonly_tx: app_readonly (SELECT-only grants) in a
READ ONLY transaction with app.org_id set from the server-side session, so even a buggy
tool can neither write nor see another org. The model only chooses a window; dates are
resolved here against clock.now() in the org's timezone, never by the model.
"""

import json
from datetime import date
from typing import Annotated, Any

from pydantic import Field, WithJsonSchema

from app import clock
from app.agents.tools.base import Tool, ToolResult, error_result
from app.agents.tools.common.args import Args, org_of
from app.db.session import readonly_tx
from app.domain.analytics.metrics import (
    DEFAULT_LIMIT,
    METRICS,
    SUMMARY_DEFINITION,
    SUMMARY_SECTIONS,
    Metric,
    activity_summary,
    compute,
)
from app.domain.analytics.windows import WindowError, WindowName, resolve_window
from app.tenancy.context import ToolContext

ACTIVITY = "Crunching the numbers…"

WINDOW_HELP = (
    "The period, resolved by the server in the business's timezone: today, yesterday, this_week "
    "(from Monday), last_7_days (today and the 6 days before), this_month, or custom with "
    "start_date and end_date. Pass the user's period here; never work out dates yourself."
)


# A date, shown to the model as a plain string: Gemini's schema subset has no "date" format.
LocalDate = Annotated[date, WithJsonSchema({"type": "string"})]


class MetricArgs(Args):
    window: WindowName = Field(default="today", description=WINDOW_HELP)
    start_date: LocalDate | None = Field(
        default=None, description="Only for window=custom: first local date, YYYY-MM-DD."
    )
    end_date: LocalDate | None = Field(
        default=None, description="Only for window=custom: last local date (inclusive), YYYY-MM-DD."
    )


class RankedMetricArgs(MetricArgs):
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=20, description="How many top entries to return.")


def _tool(metric: Metric) -> Tool:
    args_model = RankedMetricArgs if metric.ranked else MetricArgs

    async def handle(ctx: ToolContext, args: Any) -> ToolResult:
        # readonly_tx sets app.org_id/user_id/role from the session-built ctx; nothing the
        # model sent can widen it. The transaction is READ ONLY from its first statement.
        async with readonly_tx(ctx.tenant) as conn:
            org = await org_of(conn, ctx)
            try:
                window = resolve_window(args.window, org.timezone, clock.now(), args.start_date, args.end_date)
            except WindowError as exc:
                return error_result(f"{exc}. Fix the window and call {metric.name} again.")
            result = await compute(conn, org, metric.name, window, getattr(args, "limit", DEFAULT_LIMIT))
        card = {"type": "metric", **result}
        return ToolResult(
            json.dumps(result, ensure_ascii=False), cards=[card], activity_label=f"{metric.title}: {window.label}"
        )

    return Tool(
        name=metric.name,
        description=f"{metric.title} (read-only). Definition: {metric.definition}",
        args_model=args_model,
        handler=handle,
        activity_label=ACTIVITY,
    )


async def _summary(ctx: ToolContext, args: MetricArgs) -> ToolResult:
    # Same guarantees as each metric tool: readonly_tx from the session-built ctx.
    async with readonly_tx(ctx.tenant) as conn:
        org = await org_of(conn, ctx)
        try:
            window = resolve_window(args.window, org.timezone, clock.now(), args.start_date, args.end_date)
        except WindowError as exc:
            return error_result(f"{exc}. Fix the window and call activity_summary again.")
        result = await activity_summary(conn, org, window)
    # One standard metric card per section: the frontend needs no new component.
    cards = [{"type": "metric", **section} for section in result["sections"]]
    return ToolResult(
        json.dumps(result, ensure_ascii=False), cards=cards, activity_label=f"Activity summary: {window.label}"
    )


SUMMARY_TOOL = Tool(
    name="activity_summary",
    description=(
        "Activity summary (read-only): every headline metric for one window in a single call "
        f"(orders or bookings by status, revenue or cancellations, active customers, new sign-ups, "
        f"conversion rate), each with its own definition. Use it for broad questions like "
        f"'how did we do today?'. Definition: {SUMMARY_DEFINITION}"
    ),
    args_model=MetricArgs,
    handler=_summary,
    activity_label=ACTIVITY,
)

# One tool per metric, grouped by the verticals that have it (the registry keys on vertical),
# plus the summary, which every vertical with summary sections gets.
ADMIN_TOOLS: dict[str, Tool] = {name: _tool(m) for name, m in METRICS.items()} | {SUMMARY_TOOL.name: SUMMARY_TOOL}


def admin_tools_for_vertical(vertical: str) -> list[Tool]:
    summary = [SUMMARY_TOOL] if vertical in SUMMARY_SECTIONS else []
    return summary + [ADMIN_TOOLS[name] for name, m in METRICS.items() if vertical in m.verticals]
