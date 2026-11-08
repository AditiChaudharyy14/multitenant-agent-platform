"""Booking tools. Mutating ones (create, reschedule, cancel) only create a proposal;
confirm_proposal commits it, in a later turn. Identity comes from ToolContext only."""

from app.agents.tools.base import Tool, ToolResult, from_reply
from app.agents.tools.booking._common import BookingNumber, PractitionerName, ServiceName, Start
from app.agents.tools.common.args import Args, NoArgs, org_of
from app.db.session import tenant_tx
from app.domain.bookings import service
from app.domain.reply import Reply
from app.tenancy.context import ToolContext

_LABELS = {"proposed": "Summary ready", "not_found": "Booking not found", "refused": "Can't book that",
           "error": "Couldn't check"}  # fmt: skip


def _done(reply: Reply, default: str) -> ToolResult:
    return from_reply(reply, _LABELS.get(reply.status, default))


class CreateBookingArgs(Args):
    service: ServiceName
    start: Start
    practitioner: PractitionerName | None = None


async def _create(ctx: ToolContext, args: CreateBookingArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        reply = await service.propose_create(conn, ctx, await org_of(conn, ctx), service=args.service,
                                             start=args.start, practitioner=args.practitioner)  # fmt: skip
    return _done(reply, "Booking checked")


create_booking = Tool(
    name="create_booking",
    description="Prepare a new booking for the customer to confirm. It does NOT book: it re-checks the slot and "
                "returns a summary with a Confirm button. Without a practitioner it assigns a free qualified one; "
                "tell the customer who. If the slot isn't free or valid it makes no summary and returns the 3 "
                "nearest free times instead.",
    args_model=CreateBookingArgs,
    handler=_create,
    activity_label="Preparing your booking summary…",
)  # fmt: skip


class RescheduleArgs(Args):
    booking_number: BookingNumber
    new_start: Start
    practitioner: PractitionerName | None = None


async def _reschedule(ctx: ToolContext, args: RescheduleArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        reply = await service.propose_reschedule(conn, ctx, await org_of(conn, ctx), number=args.booking_number,
                                                 new_start=args.new_start, practitioner=args.practitioner)  # fmt: skip
    return _done(reply, "Booking checked")


reschedule_booking = Tool(
    name="reschedule_booking",
    description="Prepare moving one of the customer's CONFIRMED upcoming bookings to a new start (same practitioner "
                "unless one is given), for them to confirm. If the new time isn't free or valid it refuses and "
                "returns the 3 nearest free times; nothing changes.",
    args_model=RescheduleArgs,
    handler=_reschedule,
    activity_label="Checking the new time…",
)  # fmt: skip


class BookingNumberArgs(Args):
    booking_number: BookingNumber


async def _cancel(ctx: ToolContext, args: BookingNumberArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        reply = await service.propose_cancel(conn, ctx, await org_of(conn, ctx), number=args.booking_number)
    return _done(reply, "Booking checked")


cancel_booking = Tool(
    name="cancel_booking",
    description="Prepare cancelling one of the customer's CONFIRMED upcoming bookings, for them to confirm. The "
                "summary shows the fee: free more than 24 hours ahead, 50% of the price within 24 hours. Tell the "
                "customer the fee before they confirm.",
    args_model=BookingNumberArgs,
    handler=_cancel,
    activity_label="Checking your booking…",
)  # fmt: skip


async def _list(ctx: ToolContext, args: NoArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        reply = await service.list_bookings(conn, ctx, await org_of(conn, ctx))
    return _done(reply, "Bookings fetched")


list_my_bookings = Tool(
    name="list_my_bookings",
    live=True,
    description="The customer's bookings: upcoming ones, then past and cancelled ones, with booking numbers.",
    args_model=NoArgs,
    handler=_list,
    activity_label="Fetching your bookings…",
)
