"""Read-only booking tools: services and prices, opening hours, and free slots. Prices and
availability come from the database only, never from the knowledge base."""

from pydantic import Field

from app.agents.tools.base import Tool, ToolResult, from_reply
from app.agents.tools.booking._common import PractitionerName, ServiceName
from app.agents.tools.common.args import Args, NoArgs, org_of
from app.db.session import tenant_tx
from app.domain.bookings import service
from app.tenancy.context import ToolContext


async def _services(ctx: ToolContext, args: NoArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        reply = await service.list_services(conn, await org_of(conn, ctx))
    return from_reply(reply, "Services fetched")


list_services = Tool(
    name="list_services",
    description="Every service with its LIVE price, duration and which practitioners offer it. Use it for any "
                "question about prices, durations or who does what.",
    args_model=NoArgs,
    handler=_services,
    activity_label="Checking our services…",
)  # fmt: skip


async def _hours(ctx: ToolContext, args: NoArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        reply = await service.opening_hours(conn, await org_of(conn, ctx))
    return from_reply(reply, "Hours fetched")


get_opening_hours = Tool(
    name="get_opening_hours",
    description="The studio's weekly opening hours (closed days included) and upcoming holidays.",
    args_model=NoArgs,
    handler=_hours,
    activity_label="Checking opening hours…",
)


class FindSlotsArgs(Args):
    service: ServiceName
    when: str | None = Field(
        default=None, max_length=80,
        description="The customer's own words for the day, passed through unchanged: 'next Tuesday afternoon', "
                    "'tomorrow morning', 'this weekend', '15 Nov'. The server resolves it; never compute dates yourself.",
    )  # fmt: skip
    date_from: str | None = Field(default=None, max_length=10, description="YYYY-MM-DD; only when `when` isn't given.")
    date_to: str | None = Field(default=None, max_length=10, description="YYYY-MM-DD, inclusive; at most 14 days.")
    practitioner: PractitionerName | None = None


async def _find(ctx: ToolContext, args: FindSlotsArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        reply = await service.find_slots(
            conn, ctx, await org_of(conn, ctx), service=args.service, when=args.when,
            date_from=args.date_from, date_to=args.date_to, practitioner=args.practitioner,
        )  # fmt: skip
    label = {"ambiguous": "Which day?", "need_date": "Which day?", "error": "Couldn't search"}.get(reply.status, "Free times found")
    return from_reply(reply, label)


find_available_slots = Tool(
    name="find_available_slots",
    description="LIVE free start times for a service, optionally with one practitioner. Pass the customer's words "
                "as `when`; the result states the exact dates it resolved to. If it says AMBIGUOUS, ask the customer "
                "which date they mean. Without `when` or dates it searches the next 7 days.",
    args_model=FindSlotsArgs,
    handler=_find,
    activity_label="Checking availability…",
)  # fmt: skip
