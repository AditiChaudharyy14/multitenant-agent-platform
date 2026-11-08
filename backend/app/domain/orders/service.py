"""Store operations behind the order tools. Each one runs inside the caller's tenant_tx
and returns a Reply: text for the model plus cards for the UI.

Mutations never change an order here. They validate against live data and store a
proposal; app/domain/orders/commit.py carries it out after the customer confirms.
Order numbers are the only identifiers customers see; UUIDs stay internal (a proposal id
goes to the model, which needs it for confirm_proposal, but is never meant to be shown).
"""

import uuid
from collections import OrderedDict
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app import clock
from app.agents.ids import new_id
from app.domain.money import format_money, money_str, to_money
from app.domain.orders import repo
from app.domain.orders.numbers import display_number, parse_order_number
from app.domain.orders.repo import Item, Order, Product
from app.domain.proposals import repo as proposals
from app.domain.proposals.repo import PROPOSAL_TTL
from app.domain.reply import Reply
from app.domain.timefmt import local_display
from app.domain.untrusted import wrap
from app.tenancy.context import ToolContext
from app.tenancy.orgs import Org

PAYMENT_METHOD = "Cash on delivery"  # the only payment method; mocked, nothing is charged

# One text for every miss: a foreign order and a nonexistent one must be indistinguishable.
NOT_FOUND = Reply(
    "NOT FOUND: no order with that number exists on this customer's account. Ask them to "
    "check the number, or call list_my_orders to see their orders.",
    status="not_found",
)
CONFIRM_LABELS = {
    "create_order": "Confirm order", "reorder": "Confirm order",
    "update_order": "Confirm changes", "cancel_order": "Confirm cancellation",
}  # fmt: skip
EDITABLE = "pending"


# ───────────── small builders ─────────────


def line_card(sku: str, name: str, qty: int, unit_price: Decimal, currency: str) -> dict[str, Any]:
    """One order line for a card. The *_display fields use format_money, like every total."""
    return {"sku": sku, "name": name, "qty": qty, "unit_price": money_str(unit_price),
            "line_total": money_str(unit_price * qty), "unit_price_display": format_money(unit_price, currency),
            "line_total_display": format_money(unit_price * qty, currency)}  # fmt: skip


def payload_line(
    product_id: uuid.UUID, sku: str, name: str, qty: int, unit_price: Decimal, **extra: Any
) -> dict[str, Any]:
    return {"product_id": str(product_id), "sku": sku, "name": name, "qty": qty,
            "unit_price": money_str(unit_price), **extra}  # fmt: skip


def total_of(lines: list[dict[str, Any]]) -> Decimal:
    return to_money(sum((Decimal(line["unit_price"]) * line["qty"] for line in lines), Decimal(0)))


def _describe(lines: list[dict[str, Any]], currency: str) -> str:
    return "; ".join(
        f"{line['qty']} x {line['name']} ({line['sku']}) at {format_money(Decimal(line['unit_price']), currency)}"
        for line in lines
    )


def _merge(items: list[tuple[str, int]]) -> OrderedDict[str, int]:
    """Same SKU twice means the sum; SKUs are matched case-insensitively."""
    merged: OrderedDict[str, int] = OrderedDict()
    for sku, qty in items:
        key = sku.strip().upper()
        merged[key] = merged.get(key, 0) + qty
    return merged


def _unknown(skus: list[str]) -> Reply:
    return Reply(
        f"ERROR: unknown SKU(s): {', '.join(skus)}. Call search_products to find the right SKU; never guess one.",
        ok=False, status="error",
    )  # fmt: skip


async def resolve_order(
    conn: AsyncConnection, ctx: ToolContext, org: Org, raw: str, *, lock: bool = False
) -> Order | None:
    number = parse_order_number(raw, org.order_prefix or "")
    return await repo.own_order(conn, ctx, number, lock=lock) if number else None


def refuse_not_pending(org: Org, order: Order, action: str) -> Reply:
    return Reply(
        f"REFUSED: order {display_number(org.order_prefix, order.order_number)} is {order.status}, so it can no "
        f"longer be {action}. Only pending orders can be. Tell the customer its current status ({order.status}); "
        "for anything else, point them to the relevant policy and say staff can help.",
        status="refused",
    )  # fmt: skip


async def stock_issue(
    conn: AsyncConnection, ctx: ToolContext, org: Org, short: list[tuple[Product, int, int]], *, intro: str,
) -> Reply:  # fmt: skip
    """No proposal: say what is available and offer 1-2 in-stock alternatives per line.
    `short` holds (product, requested qty, available qty). Each line is also recorded as a
    stock_events row for the admin's most_out_of_stock metric."""
    await repo.record_shortages(conn, ctx, [(p.sku, requested, available) for p, requested, available in short])
    lines, text_parts = [], []
    for product, requested, available in short:
        alts = await repo.alternatives(conn, product, min_stock=requested)
        lines.append({
            "sku": product.sku, "name": product.name, "requested": requested, "available": available,
            "alternatives": [_product_card(a, org) for a in alts],
        })  # fmt: skip
        alt_text = ", ".join(
            f"{a.name} ({a.sku}, {format_money(a.price, org.currency)}, {a.stock} in stock)" for a in alts
        )
        text_parts.append(
            f"{product.name} ({product.sku}): asked for {requested}, only {available} available"
            + (f"; alternatives: {alt_text}" if alt_text else "; no in-stock alternative in that category")
        )
    return Reply(
        f"{intro} NO PROPOSAL was made. " + " | ".join(text_parts)
        + ". Tell the customer what is available and offer the alternatives; propose again once they choose.",
        cards=[{"type": "stock_issue", "lines": lines}],
        status="stock_issue",
    )  # fmt: skip


def _product_card(p: Product, org: Org) -> dict[str, Any]:
    return {"sku": p.sku, "name": p.name, "category": p.category, "price": money_str(p.price),
            "price_display": format_money(p.price, org.currency), "stock": p.stock,
            "in_stock": p.stock > 0}  # fmt: skip


async def _propose(
    conn: AsyncConnection, ctx: ToolContext, org: Org, kind: str, payload: dict[str, Any],
    card: dict[str, Any], what: str,
) -> Reply:  # fmt: skip
    """Store the proposal and build its order_summary card (with the Confirm button)."""
    pid = new_id()  # deterministic under evals: the model reads it back
    card = {
        "type": "order_summary", "kind": kind, "proposal_id": str(pid), **card,
        "payment_method": PAYMENT_METHOD,
        "expires_at": local_display(clock.now() + PROPOSAL_TTL, org.timezone),
        "actions": [{"type": "confirm_proposal", "proposal_id": str(pid), "label": CONFIRM_LABELS[kind]}],
    }  # fmt: skip
    await proposals.create(conn, ctx, kind, payload, card, pid)
    return Reply(
        f"PROPOSED, NOT DONE YET: {what} Payment: cash on delivery. Show this summary and ask the "
        "customer to confirm (Confirm button, or a reply like 'yes'). It expires in 15 minutes. "
        f"proposal_id={pid} (internal: pass it to confirm_proposal, never show it).",
        cards=[card], status="proposed",
    )  # fmt: skip


# ───────────── search ─────────────


async def search(
    conn: AsyncConnection, org: Org, *, query: str | None, category: str | None,
    min_price: Decimal | None, max_price: Decimal | None,
) -> Reply:  # fmt: skip
    found = await repo.search_products(conn, query=query, category=category, min_price=min_price, max_price=max_price)
    if not found:
        cats = ", ".join(await repo.categories(conn))
        return Reply(f"No products matched. Categories are: {cats}.", cards=[{"type": "product_list", "products": []}])
    rows = [
        f"{p.sku} | {p.name} | {p.category} | {format_money(p.price, org.currency)} | "
        + (f"{p.stock} in stock" if p.stock else "OUT OF STOCK")
        for p in found
    ]
    return Reply(
        "LIVE catalogue (SKU | name | category | price | stock):\n" + "\n".join(rows),
        cards=[{"type": "product_list", "products": [_product_card(p, org) for p in found]}],
    )


# ───────────── create / reorder ─────────────


async def propose_new_order(
    conn: AsyncConnection, ctx: ToolContext, org: Org, items: list[tuple[str, int]], address: str | None,
    *, kind: str = "create_order", changes: list[str] | None = None, extra: dict[str, Any] | None = None,
    intro: str = "",
) -> Reply:  # fmt: skip
    """create_order (and the re-quote of a stale create/reorder). If any line is short,
    nothing is proposed: the customer gets availability and alternatives instead."""
    wanted = _merge(items)
    products = await repo.products_by_sku(conn, list(wanted))
    if missing := [s for s in wanted if s not in products]:
        return _unknown(missing)
    short = [(products[s], q, products[s].stock) for s, q in wanted.items() if q > products[s].stock]
    if short:
        return await stock_issue(conn, ctx, org, short, intro=intro or "Not enough stock.")
    address = (address or "").strip() or await repo.last_address(conn, ctx)
    if not address:
        return Reply("NEED ADDRESS: this customer has no previous order, so ask for their delivery address, "
                     "then call create_order again with delivery_address.", status="need_address")  # fmt: skip
    lines = [payload_line(products[s].id, s, products[s].name, q, products[s].price) for s, q in wanted.items()]
    total = total_of(lines)
    payload = {"lines": lines, "delivery_address": address, "total": money_str(total), **(extra or {})}
    card = {
        "lines": [line_card(x["sku"], x["name"], x["qty"], Decimal(x["unit_price"]), org.currency) for x in lines],
        "total": money_str(total), "total_display": format_money(total, org.currency),
        "delivery_address": address, "changes": changes or [], "unavailable": [], **(extra or {}),
    }  # fmt: skip
    what = (
        f"{intro + ' ' if intro else ''}new order of {_describe(lines, org.currency)}; total "
        f"{format_money(total, org.currency)}; deliver to {wrap('delivery_address', address)}."
    )
    if changes:
        what += " Changes: " + " ".join(changes)
    return await _propose(conn, ctx, org, kind, payload, card, what)


async def propose_reorder(conn: AsyncConnection, ctx: ToolContext, org: Org, raw_number: str | None) -> Reply:
    if raw_number:
        source = await resolve_order(conn, ctx, org, raw_number)
        if source is None:
            return NOT_FOUND
    else:
        source = await repo.latest_reorderable(conn, ctx)
        if source is None:
            return Reply("NO PREVIOUS ORDER: this customer has no earlier (non-cancelled) order to repeat. "
                         "Offer to help them choose products instead.", status="no_order")  # fmt: skip
    source_number = display_number(org.order_prefix, source.order_number)
    old = await repo.items(conn, source.id)
    live = await repo.products_by_sku(conn, [i.sku for i in old])
    lines, changes, unavailable = [], [], []
    shortages: list[tuple[str, int, int]] = []  # (sku, wanted, in stock) for stock_events
    for item in old:
        p = live[item.sku]  # order_items.product_id is a FK, so the product still exists
        if p.price != item.unit_price:
            changes.append(f"{p.name}: price changed from {format_money(item.unit_price, org.currency)} "
                           f"to {format_money(p.price, org.currency)}.")  # fmt: skip
        if p.stock < item.qty:
            shortages.append((p.sku, item.qty, p.stock))
        if p.stock == 0:
            alts = await repo.alternatives(conn, p, min_stock=item.qty)
            unavailable.append({"sku": p.sku, "name": p.name, "requested": item.qty, "available": 0,
                                "alternatives": [_product_card(a, org) for a in alts]})  # fmt: skip
            alt_text = ", ".join(f"{a.name} ({a.sku})" for a in alts) or "none in that category"
            changes.append(f"{p.name} is out of stock, so it is left out (alternatives: {alt_text}).")
            continue
        qty = item.qty
        if p.stock < item.qty:
            qty = p.stock
            changes.append(f"{p.name}: only {p.stock} in stock (you had {item.qty}), so the quantity is {qty}.")
        lines.append(payload_line(p.id, p.sku, p.name, qty, p.price))
    await repo.record_shortages(conn, ctx, shortages)
    if not lines:
        return Reply(
            f"NOTHING TO REORDER from {source_number}: every item is out of stock. " + " ".join(changes)
            + " Offer the alternatives instead. NO PROPOSAL was made.",
            cards=[{"type": "stock_issue", "lines": unavailable}], status="stock_issue",
        )  # fmt: skip
    total = total_of(lines)
    payload = {"lines": lines, "delivery_address": source.delivery_address, "total": money_str(total),
               "source_order_number": source_number}  # fmt: skip
    card = {
        "lines": [line_card(x["sku"], x["name"], x["qty"], Decimal(x["unit_price"]), org.currency) for x in lines],
        "total": money_str(total), "total_display": format_money(total, org.currency),
        "delivery_address": source.delivery_address, "source_order_number": source_number,
        "changes": changes, "unavailable": unavailable,
    }  # fmt: skip
    what = (f"repeat of {source_number}: {_describe(lines, org.currency)}; total {format_money(total, org.currency)}; "
            f"deliver to {wrap('delivery_address', source.delivery_address)}. "
            + ("Tell the customer about EVERY change: " + " ".join(changes) if changes else "Nothing changed since then."))  # fmt: skip
    return await _propose(conn, ctx, org, "reorder", payload, card, what)


# ───────────── update / cancel ─────────────


async def propose_update(
    conn: AsyncConnection, ctx: ToolContext, org: Org, raw_number: str,
    items: list[tuple[str, int]] | None, address: str | None, *, intro: str = "",
) -> Reply:  # fmt: skip
    address = (address or "").strip() or None
    if not items and not address:
        return Reply("ERROR: nothing to change. Pass items (new quantity per SKU, 0 removes) and/or "
                     "delivery_address.", ok=False, status="error")  # fmt: skip
    order = await resolve_order(conn, ctx, org, raw_number)
    if order is None:
        return NOT_FOUND
    if order.status != EDITABLE:
        return refuse_not_pending(org, order, "changed")
    number = display_number(org.order_prefix, order.order_number)
    current = {i.sku: i for i in await repo.items(conn, order.id)}
    new_qty = {sku: i.qty for sku, i in current.items()}
    for sku, qty in items or []:
        new_qty[sku.strip().upper()] = qty  # the new quantity for that SKU; 0 removes it
    products = await repo.products_by_sku(conn, list(new_qty))
    if missing := [s for s in new_qty if s not in products]:
        return _unknown(missing)
    if not any(q > 0 for q in new_qty.values()):
        return Reply(f"REFUSED: that would leave {number} with no items. To remove everything, cancel the "
                     "order instead (cancel_order).", status="refused")  # fmt: skip
    # Units already held by this order count as available to it.
    short = [
        (products[s], q, products[s].stock + (current[s].qty if s in current else 0))
        for s, q in new_qty.items()
        if q - (current[s].qty if s in current else 0) > products[s].stock
    ]
    if short:
        return await stock_issue(conn, ctx, org, short, intro=intro or f"Not enough stock to change {number}.")

    # Lines already on the order keep their snapshot price; new SKUs take today's price.
    lines = [
        payload_line(products[s].id, s, (current[s].name if s in current else products[s].name), q,
                     current[s].unit_price if s in current else products[s].price, existing=s in current)
        for s, q in new_qty.items() if q > 0
    ]  # fmt: skip
    diff = [
        {"sku": s, "name": products[s].name, "old_qty": current[s].qty if s in current else 0, "new_qty": q}
        for s, q in new_qty.items() if q != (current[s].qty if s in current else 0)
    ]  # fmt: skip
    new_address = address or order.delivery_address
    if not diff and new_address == order.delivery_address:
        return Reply(f"NO CHANGE: {number} already looks like that.", status="no_change")
    total = total_of(lines)
    payload = {
        "order_id": str(order.id), "order_number": number, "lines": lines, "delivery_address": new_address,
        "total": money_str(total),
        # What the order looked like when proposed; the commit refuses if it moved since.
        "expected_items": {s: i.qty for s, i in current.items()}, "expected_address": order.delivery_address,
    }  # fmt: skip
    card = {
        "order_number": number,
        "lines": [line_card(x["sku"], x["name"], x["qty"], Decimal(x["unit_price"]), org.currency) for x in lines],
        "diff": diff, "old_total": money_str(order.total), "total": money_str(total),
        "total_display": format_money(total, org.currency),
        "old_delivery_address": order.delivery_address, "delivery_address": new_address,
        "changes": [], "unavailable": [],
    }  # fmt: skip
    parts = [f"{d['name']} {d['old_qty']} -> {d['new_qty']}" for d in diff]
    if new_address != order.delivery_address:
        parts.append(
            f"address {wrap('delivery_address', order.delivery_address)} -> {wrap('delivery_address', new_address)}"
        )
    what = (f"{intro + ' ' if intro else ''}change {number}: {', '.join(parts)}. New total "
            f"{format_money(total, org.currency)} (was {format_money(order.total, org.currency)}).")  # fmt: skip
    return await _propose(conn, ctx, org, "update_order", payload, card, what)


async def propose_cancel(conn: AsyncConnection, ctx: ToolContext, org: Org, raw_number: str) -> Reply:
    order = await resolve_order(conn, ctx, org, raw_number)
    if order is None:
        return NOT_FOUND
    if order.status != EDITABLE:
        return refuse_not_pending(org, order, "cancelled")
    number = display_number(org.order_prefix, order.order_number)
    current = await repo.items(conn, order.id)
    payload = {"order_id": str(order.id), "order_number": number}
    card = {
        "order_number": number, "lines": [line_card(i.sku, i.name, i.qty, i.unit_price, org.currency) for i in current],
        "total": money_str(order.total), "total_display": format_money(order.total, org.currency),
        "delivery_address": order.delivery_address, "changes": [], "unavailable": [],
    }  # fmt: skip
    return await _propose(conn, ctx, org, "cancel_order", payload, card,
                          f"cancel {number} ({format_money(order.total, org.currency)}).")  # fmt: skip


# ───────────── read-only ─────────────


async def order_status(conn: AsyncConnection, ctx: ToolContext, org: Org, raw_number: str) -> Reply:
    order = await resolve_order(conn, ctx, org, raw_number)
    if order is None:
        return NOT_FOUND
    number = display_number(org.order_prefix, order.order_number)
    lines = await repo.items(conn, order.id)
    steps = await repo.history(conn, order.id)
    card: dict[str, Any] = {
        "type": "order_status", "order_number": number, "status": order.status,
        "placed_at": local_display(order.created_at, org.timezone),
        "items": [line_card(i.sku, i.name, i.qty, i.unit_price, org.currency) for i in lines],
        "total": money_str(order.total), "total_display": format_money(order.total, org.currency),
        "delivery_address": order.delivery_address, "payment_method": PAYMENT_METHOD,
        "can_edit": order.status == EDITABLE,
        "history": [{"from_status": h.from_status, "to_status": h.to_status,
                     "at": local_display(h.changed_at, org.timezone)} for h in steps],
    }  # fmt: skip
    timeline = "; ".join(f"{h['to_status']} at {h['at']}" for h in card["history"])
    return Reply(
        f"Order {number}: status {order.status}. Items: "
        + "; ".join(f"{i.qty} x {i.name}" for i in lines)
        + f". Total {card['total_display']} ({PAYMENT_METHOD}). Deliver to {wrap('delivery_address', order.delivery_address)}. "
        f"History ({org.timezone}): {timeline}. "
        + (
            "It can still be changed or cancelled." if card["can_edit"] else "It can no longer be changed or cancelled."
        ),
        cards=[card],
    )


async def order_rows(conn: AsyncConnection, ctx: ToolContext, org: Org) -> list[dict[str, Any]]:
    """The caller's orders, newest first, as display rows. The ONE source for both the
    list_my_orders tool and the My orders panel (GET /api/orders), so they can't disagree."""
    return [
        {"order_number": display_number(org.order_prefix, o.order_number), "status": o.status,
         "total": money_str(o.total), "total_display": format_money(o.total, org.currency),
         "placed_at": local_display(o.created_at, org.timezone), "units": units}
        for o, units in await repo.list_own(conn, ctx)
    ]  # fmt: skip


async def list_orders(conn: AsyncConnection, ctx: ToolContext, org: Org) -> Reply:
    orders = await order_rows(conn, ctx, org)
    if not orders:
        return Reply("This customer has no orders yet.", cards=[{"type": "order_list", "orders": []}])
    text = "\n".join(f"{o['order_number']} | {o['placed_at']} | {o['status']} | {o['total_display']}" for o in orders)
    return Reply("Orders, newest first (number | placed | status | total):\n" + text,
                 cards=[{"type": "order_list", "orders": orders}])  # fmt: skip


def as_items(rows: list[Any]) -> list[Item]:  # helper for committers
    return [Item(uuid.UUID(r["product_id"]), r["sku"], r["name"], Decimal(r["unit_price"]), r["qty"]) for r in rows]
