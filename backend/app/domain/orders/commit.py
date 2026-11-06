"""Committers: carry out a confirmed store proposal inside the confirm transaction.

Each re-checks live facts under row locks before writing anything:
  - create_order / reorder: lock the products (FOR UPDATE, id order). If a price moved or
    stock no longer covers a line, nothing is written: the customer gets a fresh proposal
    (or, when stock is short, availability and alternatives).
  - update_order / cancel_order: lock the order (FOR UPDATE) and re-check it is pending
    and unchanged since the proposal.
Stock moves only through repo.take_stock (atomic, guarded) and repo.restore_stock. Any
guarded decrement that matches no row raises, and the whole transaction rolls back.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.money import format_money, money_str
from app.domain.orders import repo, service
from app.domain.orders.numbers import display_number
from app.domain.orders.repo import Item, OutOfStock
from app.domain.proposals.repo import Proposal
from app.domain.proposals.service import CommitAborted, Committer
from app.domain.reply import Reply
from app.tenancy.context import ToolContext
from app.tenancy.orgs import Org, get_org_by_id

CHANGED = "CHANGED: something changed since the summary, so NOTHING was committed."
ABORTED = Reply("REFUSED: an item sold out at the last moment, so nothing was placed. Offer to check "
                "availability again.", status="refused")  # fmt: skip


async def _org(conn: AsyncConnection, ctx: ToolContext) -> Org:
    org = await get_org_by_id(conn, ctx.org_id)
    assert org is not None  # the session's org always exists (FK)
    return org


def _changed(fresh: Reply, changes: list[str]) -> Reply:
    """Wrap the re-quote: same cards (a fresh proposal, or a stock issue), status 'changed'."""
    return Reply(f"{CHANGED} {' '.join(changes)} {fresh.text}", cards=fresh.cards, ok=fresh.ok, status="changed")


async def commit_new_order(conn: AsyncConnection, ctx: ToolContext, proposal: Proposal) -> Reply:
    org = await _org(conn, ctx)
    p = proposal.payload
    lines = service.as_items(p["lines"])
    live = await repo.products_by_sku(conn, [i.sku for i in lines], lock=True)
    changes = []
    for i in lines:
        product = live[i.sku]
        if product.price != i.unit_price:
            changes.append(f"{i.name}: price changed from {format_money(i.unit_price, org.currency)} to "
                           f"{format_money(product.price, org.currency)}.")  # fmt: skip
        if product.stock < i.qty:
            changes.append(f"{i.name}: only {product.stock} left (summary had {i.qty}).")
    if changes:
        # Re-quote at today's prices; this supersedes the old proposal in the same transaction.
        extra = {"source_order_number": p["source_order_number"]} if "source_order_number" in p else None
        fresh = await service.propose_new_order(
            conn, ctx, org, [(i.sku, i.qty) for i in lines], p["delivery_address"],
            kind=proposal.kind, changes=changes, extra=extra, intro="Updated summary:",
        )  # fmt: skip
        return _changed(fresh, changes)

    try:
        for i in lines:
            await repo.take_stock(conn, live[i.sku].id, i.qty)
    except OutOfStock:
        raise CommitAborted(ABORTED) from None  # unreachable while the rows are locked; belt and braces
    total = Decimal(p["total"])
    order_id, number = await repo.insert_order(conn, ctx, address=p["delivery_address"], total=total,
                                               proposal_id=proposal.id)  # fmt: skip
    for i in lines:
        await repo.insert_item(conn, ctx.org_id, ctx.user_id, order_id, i)
    await repo.insert_history(conn, ctx.org_id, ctx.user_id, order_id, None, "pending", "customer")
    shown = display_number(org.order_prefix, number)
    card = {
        "type": "order_confirmed", "order_number": shown, "status": "pending",
        "lines": [service.line_card(i.sku, i.name, i.qty, i.unit_price) for i in lines],
        "total": money_str(total), "total_display": format_money(total, org.currency),
        "delivery_address": p["delivery_address"], "payment_method": service.PAYMENT_METHOD,
    }  # fmt: skip
    return Reply(
        f"CONFIRMED: order {shown} is placed with status pending, total {card['total_display']}, cash on "
        f"delivery, to {p['delivery_address']}. Give the customer the order number {shown}.",
        cards=[card], status="committed",
    )  # fmt: skip


async def _locked_pending(conn: AsyncConnection, ctx: ToolContext, org: Org, proposal: Proposal, action: str):
    """The order, locked; or a Reply refusing because it is gone or no longer pending."""
    order = await repo.lock_own_order_by_id(conn, ctx, uuid.UUID(proposal.payload["order_id"]))
    if order is None:
        return service.NOT_FOUND
    if order.status != service.EDITABLE:
        return service.refuse_not_pending(org, order, action)
    return order


async def commit_update(conn: AsyncConnection, ctx: ToolContext, proposal: Proposal) -> Reply:
    org = await _org(conn, ctx)
    p = proposal.payload
    order = await _locked_pending(conn, ctx, org, proposal, "changed")
    if isinstance(order, Reply):
        return order
    number = display_number(org.order_prefix, order.order_number)
    current = {i.sku: i for i in await repo.items(conn, order.id)}
    if {s: i.qty for s, i in current.items()} != p["expected_items"] or order.delivery_address != p["expected_address"]:
        return Reply(f"{CHANGED} Order {number} was changed after this summary was made. Show the customer "
                     "its current state (get_order_status) and propose again.", status="changed")  # fmt: skip

    lines = service.as_items(p["lines"])
    wanted = {i.sku: i for i in lines}
    live = await repo.products_by_sku(conn, list(set(current) | set(wanted)), lock=True)
    changes = [
        f"{i.name}: price changed from {format_money(i.unit_price, org.currency)} to "
        f"{format_money(live[i.sku].price, org.currency)}."
        for i in lines if i.sku not in current and live[i.sku].price != i.unit_price
    ]  # fmt: skip
    changes += [
        f"{i.name}: only {live[i.sku].stock + (current[i.sku].qty if i.sku in current else 0)} available now."
        for i in lines if i.qty - (current[i.sku].qty if i.sku in current else 0) > live[i.sku].stock
    ]  # fmt: skip
    if changes:
        requested = [(s, wanted[s].qty if s in wanted else 0) for s in set(current) | set(wanted)]
        fresh = await service.propose_update(conn, ctx, org, number, requested, p["delivery_address"],
                                             intro="Updated summary:")  # fmt: skip
        return _changed(fresh, changes)

    try:
        for sku in sorted(set(current) | set(wanted)):
            old_qty = current[sku].qty if sku in current else 0
            new_qty = wanted[sku].qty if sku in wanted else 0
            delta = new_qty - old_qty
            if delta > 0:
                await repo.take_stock(conn, live[sku].id, delta)  # more units: atomic, guarded
            elif delta < 0:
                await repo.restore_stock(conn, live[sku].id, -delta)  # fewer units: back on the shelf
            if new_qty == 0:
                await repo.delete_item(conn, order.id, sku)
            elif old_qty == 0:
                await repo.insert_item(conn, ctx.org_id, ctx.user_id, order.id, wanted[sku])
            elif delta:
                await repo.set_item_qty(conn, order.id, sku, new_qty)
    except OutOfStock:
        raise CommitAborted(ABORTED) from None
    total = Decimal(p["total"])
    await repo.update_order(conn, order.id, address=p["delivery_address"], total=total)
    card = {
        "type": "order_updated", "order_number": number, "status": order.status,
        "lines": [service.line_card(i.sku, i.name, i.qty, i.unit_price) for i in lines],
        "total": money_str(total), "total_display": format_money(total, org.currency),
        "delivery_address": p["delivery_address"],
    }  # fmt: skip
    return Reply(f"CONFIRMED: order {number} is updated. New total {card['total_display']}, delivering to "
                 f"{p['delivery_address']}. It is still pending.", cards=[card], status="committed")  # fmt: skip


async def commit_cancel(conn: AsyncConnection, ctx: ToolContext, proposal: Proposal) -> Reply:
    org = await _org(conn, ctx)
    order = await _locked_pending(conn, ctx, org, proposal, "cancelled")
    if isinstance(order, Reply):
        return order
    number = display_number(org.order_prefix, order.order_number)
    held: list[Item] = await repo.items(conn, order.id)
    for i in sorted(held, key=lambda x: x.product_id):  # id order, like every other product lock
        await repo.restore_stock(conn, i.product_id, i.qty)
    await repo.set_status(conn, ctx.org_id, order, "cancelled", "customer")
    card: dict[str, Any] = {"type": "order_cancelled", "order_number": number, "status": "cancelled",
                            "total_display": format_money(order.total, org.currency)}  # fmt: skip
    return Reply(f"CONFIRMED: order {number} is cancelled. Nothing is owed (cash on delivery).",
                 cards=[card], status="committed")  # fmt: skip


COMMITTERS: dict[str, Committer] = {
    "create_order": commit_new_order,
    "reorder": commit_new_order,
    "update_order": commit_update,
    "cancel_order": commit_cancel,
}
