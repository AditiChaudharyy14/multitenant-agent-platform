"""Order tools. Mutating ones (create, update, cancel, reorder) only create a proposal;
confirm_proposal commits it, in a later turn. Identity comes from ToolContext only."""

from pydantic import Field

from app.agents.tools.base import Tool, ToolResult, from_reply
from app.agents.tools.store._common import Address, Args, OrderNumber, Sku, org_of
from app.db.session import tenant_tx
from app.domain.orders import service
from app.domain.reply import Reply
from app.tenancy.context import ToolContext

_LABELS = {"proposed": "Summary ready", "not_found": "Order not found", "refused": "Can't do that",
           "stock_issue": "Not enough stock", "need_address": "Need an address"}  # fmt: skip


def _done(reply: Reply, default: str) -> ToolResult:
    return from_reply(reply, _LABELS.get(reply.status, default))


# ───────────── create_order ─────────────


class NewItem(Args):
    sku: Sku
    qty: int = Field(ge=1, le=50)


class CreateOrderArgs(Args):
    items: list[NewItem] = Field(min_length=1, max_length=20)
    delivery_address: Address | None = Field(
        default=None, description="Omit to reuse the address of the customer's latest order."
    )


async def _create(ctx: ToolContext, args: CreateOrderArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        org = await org_of(conn, ctx)
        reply = await service.propose_new_order(
            conn, ctx, org, [(i.sku, i.qty) for i in args.items], args.delivery_address
        )
    return _done(reply, "Order checked")


create_order = Tool(
    name="create_order",
    description=(
        "Prepare a new order for the customer to confirm. It does NOT place the order: it checks live "
        "price and stock and returns a summary with a Confirm button. If an item is short it makes no "
        "summary and returns alternatives instead. Use exact SKUs from search_products."
    ),
    args_model=CreateOrderArgs,
    handler=_create,
    activity_label="Preparing your order summary…",
)


# ───────────── update_order ─────────────


class ChangedItem(Args):
    sku: Sku
    qty: int = Field(ge=0, le=50, description="The NEW total quantity of this SKU on the order; 0 removes it.")


class UpdateOrderArgs(Args):
    order_number: OrderNumber
    items: list[ChangedItem] | None = Field(
        default=None, max_length=20, description="Only the SKUs that change; others stay as they are."
    )
    delivery_address: Address | None = None


async def _update(ctx: ToolContext, args: UpdateOrderArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        org = await org_of(conn, ctx)
        items = [(i.sku, i.qty) for i in args.items] if args.items else None
        reply = await service.propose_update(conn, ctx, org, args.order_number, items, args.delivery_address)
    return _done(reply, "Order checked")


update_order = Tool(
    name="update_order",
    description=(
        "Prepare a change to a PENDING order (quantities, added or removed items, delivery address) for "
        "the customer to confirm. Orders that are confirmed, shipped, delivered or cancelled can't be "
        "changed; the tool refuses and gives the current status."
    ),
    args_model=UpdateOrderArgs,
    handler=_update,
    activity_label="Preparing the changes…",
)


# ───────────── cancel_order ─────────────


class OrderNumberArgs(Args):
    order_number: OrderNumber


async def _cancel(ctx: ToolContext, args: OrderNumberArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        org = await org_of(conn, ctx)
        reply = await service.propose_cancel(conn, ctx, org, args.order_number)
    return _done(reply, "Order checked")


cancel_order = Tool(
    name="cancel_order",
    description="Prepare the cancellation of a PENDING order for the customer to confirm. Other statuses are refused.",
    args_model=OrderNumberArgs,
    handler=_cancel,
    activity_label="Checking your order…",
)


# ───────────── get_order_status / list_my_orders ─────────────


async def _status(ctx: ToolContext, args: OrderNumberArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        org = await org_of(conn, ctx)
        reply = await service.order_status(conn, ctx, org, args.order_number)
    return _done(reply, "Order found")


get_order_status = Tool(
    name="get_order_status",
    description="Live status of one of the customer's orders: items, total, address and status history.",
    args_model=OrderNumberArgs,
    handler=_status,
    activity_label="Looking up your order…",
)


class NoArgs(Args):
    pass


async def _list(ctx: ToolContext, args: NoArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        org = await org_of(conn, ctx)
        reply = await service.list_orders(conn, ctx, org)
    return _done(reply, "Orders fetched")


list_my_orders = Tool(
    name="list_my_orders",
    description="The customer's orders, newest first, with number, date, status and total.",
    args_model=NoArgs,
    handler=_list,
    activity_label="Fetching your orders…",
)


# ───────────── reorder ─────────────


class ReorderArgs(Args):
    order_number: OrderNumber | None = Field(
        default=None, description="Omit to repeat the latest order that wasn't cancelled."
    )


async def _reorder(ctx: ToolContext, args: ReorderArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        org = await org_of(conn, ctx)
        reply = await service.propose_reorder(conn, ctx, org, args.order_number)
    return _done(reply, "Order checked")


reorder = Tool(
    name="reorder",
    description=(
        "Prepare a repeat of an earlier order ('same as last time') at TODAY's prices and stock, for the "
        "customer to confirm. The result lists every change (price differences, items short or out of "
        "stock, with alternatives); tell the customer about each one."
    ),
    args_model=ReorderArgs,
    handler=_reorder,
    activity_label="Checking your last order…",
)
