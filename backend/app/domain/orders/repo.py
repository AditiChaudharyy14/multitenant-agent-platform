"""Order and product SQL, always inside the caller's tenant_tx.

RLS already scopes everything to the org (and a customer to their own orders). Queries
on customer-owned rows also filter on user_id = ctx.user_id explicitly, so a policy
mistake alone can't leak another customer's order. All "now" comes from clock.now().
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app import clock
from app.tenancy.context import ToolContext


class OutOfStock(Exception):
    """A guarded stock decrement matched no row. The transaction must roll back."""


@dataclass(frozen=True, slots=True)
class Product:
    id: uuid.UUID
    sku: str
    name: str
    category: str
    price: Decimal
    stock: int


@dataclass(frozen=True, slots=True)
class Order:
    id: uuid.UUID
    user_id: uuid.UUID
    order_number: int
    status: str
    delivery_address: str
    total: Decimal
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Item:
    product_id: uuid.UUID
    sku: str
    name: str
    unit_price: Decimal  # snapshot taken when the line was ordered
    qty: int


_PRODUCT = "id, sku, name, category, price, stock"
_ORDER = "id, user_id, order_number, status, delivery_address, total, created_at"


# ───────────── products ─────────────


async def products_by_sku(conn: AsyncConnection, skus: list[str], *, lock: bool = False) -> dict[str, Product]:
    """Live rows by SKU. lock=True takes FOR UPDATE in id order, so two commits touching
    the same products always lock them in the same order and can't deadlock."""
    if not skus:
        return {}
    sql = f"SELECT {_PRODUCT} FROM products WHERE sku = ANY(:skus) ORDER BY id"
    rows = await conn.execute(text(sql + (" FOR UPDATE" if lock else "")), {"skus": list(set(skus))})
    return {r.sku: Product(*r) for r in rows}


async def search_products(
    conn: AsyncConnection, *, query: str | None, category: str | None,
    min_price: Decimal | None, max_price: Decimal | None, limit: int = 10,
) -> list[Product]:  # fmt: skip
    clauses, params = [], {"n": limit}
    if query:
        # Every word must appear in the name, SKU or category (case-insensitive).
        for i, word in enumerate(query.split()[:6]):
            clauses.append(f"(name ILIKE :w{i} OR sku ILIKE :w{i} OR category ILIKE :w{i})")
            params[f"w{i}"] = f"%{_escape_like(word)}%"
    if category:
        clauses.append("lower(category) = lower(:cat)")
        params["cat"] = category
    if min_price is not None:
        clauses.append("price >= :lo")
        params["lo"] = min_price
    if max_price is not None:
        clauses.append("price <= :hi")
        params["hi"] = max_price
    where = " AND ".join(clauses) or "true"
    rows = await conn.execute(
        text(f"SELECT {_PRODUCT} FROM products WHERE {where} ORDER BY category, price, sku LIMIT :n"), params  # noqa: S608
    )
    return [Product(*r) for r in rows]


async def categories(conn: AsyncConnection) -> list[str]:
    return list(await conn.scalars(text("SELECT DISTINCT category FROM products ORDER BY category")))


async def alternatives(conn: AsyncConnection, of: Product, min_stock: int, limit: int = 2) -> list[Product]:
    """In-stock products from the same category, nearest in price first."""
    rows = await conn.execute(
        text(
            f"SELECT {_PRODUCT} FROM products WHERE category = :c AND id <> :id AND stock >= :q "
            "ORDER BY abs(price - :p), price, sku LIMIT :n"
        ),
        {"c": of.category, "id": of.id, "q": max(min_stock, 1), "p": of.price, "n": limit},
    )
    return [Product(*r) for r in rows]


async def record_shortages(conn: AsyncConnection, ctx: ToolContext, short: list[tuple[str, int, int]]) -> None:
    """One stock_events row per (sku, requested, available) line a customer couldn't get:
    the admin metric most_out_of_stock counts them. Written in the tool's own transaction."""
    rows = [
        {"o": ctx.org_id, "u": ctx.user_id, "s": sku, "r": requested, "a": available,
         "c": ctx.conversation_id, "t": clock.now()}
        for sku, requested, available in short if requested > available
    ]  # fmt: skip
    if rows:
        await conn.execute(
            text("INSERT INTO stock_events (org_id, user_id, sku, requested_qty, available_qty, conversation_id, created_at) "
                 "VALUES (:o, :u, :s, :r, :a, :c, :t)"),
            rows,
        )  # fmt: skip


async def take_stock(conn: AsyncConnection, product_id: uuid.UUID, qty: int) -> None:
    """Atomic, guarded decrement. Zero rows means not enough stock: abort everything."""
    res = await conn.execute(
        text("UPDATE products SET stock = stock - :q WHERE id = :id AND stock >= :q"), {"id": product_id, "q": qty}
    )
    if res.rowcount != 1:
        raise OutOfStock(str(product_id))


async def restore_stock(conn: AsyncConnection, product_id: uuid.UUID, qty: int) -> None:
    await conn.execute(text("UPDATE products SET stock = stock + :q WHERE id = :id"), {"id": product_id, "q": qty})


# ───────────── orders (the caller's own) ─────────────


async def own_order(conn: AsyncConnection, ctx: ToolContext, number: int, *, lock: bool = False) -> Order | None:
    sql = f"SELECT {_ORDER} FROM orders WHERE org_id = :o AND user_id = :u AND order_number = :n"
    row = (
        await conn.execute(text(sql + (" FOR UPDATE" if lock else "")), {"o": ctx.org_id, "u": ctx.user_id, "n": number})
    ).one_or_none()
    return Order(*row) if row else None


async def lock_own_order_by_id(conn: AsyncConnection, ctx: ToolContext, order_id: uuid.UUID) -> Order | None:
    row = (
        await conn.execute(
            text(f"SELECT {_ORDER} FROM orders WHERE id = :id AND org_id = :o AND user_id = :u FOR UPDATE"),
            {"id": order_id, "o": ctx.org_id, "u": ctx.user_id},
        )
    ).one_or_none()
    return Order(*row) if row else None


async def latest_reorderable(conn: AsyncConnection, ctx: ToolContext) -> Order | None:
    row = (
        await conn.execute(
            text(
                f"SELECT {_ORDER} FROM orders WHERE org_id = :o AND user_id = :u AND status <> 'cancelled' "
                "ORDER BY created_at DESC, order_number DESC LIMIT 1"
            ),
            {"o": ctx.org_id, "u": ctx.user_id},
        )
    ).one_or_none()
    return Order(*row) if row else None


async def list_own(conn: AsyncConnection, ctx: ToolContext, limit: int = 20) -> list[tuple[Order, int]]:
    """(order, item count), newest first."""
    rows = await conn.execute(
        text(
            f"SELECT {', '.join('o.' + c.strip() for c in _ORDER.split(','))}, "
            "(SELECT coalesce(sum(qty), 0) FROM order_items i WHERE i.order_id = o.id) AS units "
            "FROM orders o WHERE o.org_id = :o AND o.user_id = :u "
            "ORDER BY o.created_at DESC, o.order_number DESC LIMIT :n"
        ),
        {"o": ctx.org_id, "u": ctx.user_id, "n": limit},
    )
    return [(Order(*r[:-1]), int(r[-1])) for r in rows]


async def last_address(conn: AsyncConnection, ctx: ToolContext) -> str | None:
    return await conn.scalar(
        text(
            "SELECT delivery_address FROM orders WHERE org_id = :o AND user_id = :u "
            "ORDER BY created_at DESC, order_number DESC LIMIT 1"
        ),
        {"o": ctx.org_id, "u": ctx.user_id},
    )


async def items(conn: AsyncConnection, order_id: uuid.UUID) -> list[Item]:
    rows = await conn.execute(
        text("SELECT product_id, sku, name, unit_price, qty FROM order_items WHERE order_id = :id ORDER BY sku"),
        {"id": order_id},
    )
    return [Item(*r) for r in rows]


async def history(conn: AsyncConnection, order_id: uuid.UUID) -> list[Any]:
    rows = await conn.execute(
        text(
            "SELECT from_status, to_status, changed_at, changed_by FROM order_status_history "
            "WHERE order_id = :id ORDER BY changed_at, id"
        ),
        {"id": order_id},
    )
    return list(rows)


# ───────────── writes ─────────────


async def insert_order(
    conn: AsyncConnection, ctx: ToolContext, *, address: str, total: Decimal, proposal_id: uuid.UUID,
) -> tuple[uuid.UUID, int]:  # fmt: skip
    """A new pending order; the trigger assigns its order number."""
    now = clock.now()
    row = (
        await conn.execute(
            text(
                "INSERT INTO orders (org_id, user_id, status, delivery_address, payment_method, total, "
                "conversation_id, proposal_id, created_at, updated_at) "
                "VALUES (:o, :u, 'pending', :a, 'cod', :t, :c, :p, :now, :now) RETURNING id, order_number"
            ),
            {"o": ctx.org_id, "u": ctx.user_id, "a": address, "t": total, "c": ctx.conversation_id,
             "p": proposal_id, "now": now},  # fmt: skip
        )
    ).one()
    return row.id, row.order_number


async def insert_item(conn: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID, order_id: uuid.UUID, item: Item) -> None:
    await conn.execute(
        text(
            "INSERT INTO order_items (org_id, user_id, order_id, product_id, sku, name, unit_price, qty) "
            "VALUES (:o, :u, :ord, :p, :sku, :n, :price, :q)"
        ),
        {"o": org_id, "u": user_id, "ord": order_id, "p": item.product_id, "sku": item.sku, "n": item.name,
         "price": item.unit_price, "q": item.qty},  # fmt: skip
    )


async def set_item_qty(conn: AsyncConnection, order_id: uuid.UUID, sku: str, qty: int) -> None:
    await conn.execute(
        text("UPDATE order_items SET qty = :q WHERE order_id = :id AND sku = :sku"), {"id": order_id, "sku": sku, "q": qty}
    )


async def delete_item(conn: AsyncConnection, order_id: uuid.UUID, sku: str) -> None:
    await conn.execute(text("DELETE FROM order_items WHERE order_id = :id AND sku = :sku"), {"id": order_id, "sku": sku})


async def update_order(conn: AsyncConnection, order_id: uuid.UUID, *, address: str, total: Decimal) -> None:
    await conn.execute(
        text("UPDATE orders SET delivery_address = :a, total = :t, updated_at = :now WHERE id = :id"),
        {"id": order_id, "a": address, "t": total, "now": clock.now()},
    )


async def set_status(
    conn: AsyncConnection, org_id: uuid.UUID, order: Order, to_status: str, changed_by: str,
) -> None:  # fmt: skip
    """Every transition writes order_status_history in the same transaction."""
    now = clock.now()
    await conn.execute(
        text("UPDATE orders SET status = :s, updated_at = :now WHERE id = :id"), {"s": to_status, "now": now, "id": order.id}
    )
    await insert_history(conn, org_id, order.user_id, order.id, order.status, to_status, changed_by)


async def insert_history(
    conn: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID, order_id: uuid.UUID,
    from_status: str | None, to_status: str, changed_by: str,
) -> None:  # fmt: skip
    await conn.execute(
        text(
            "INSERT INTO order_status_history (org_id, user_id, order_id, from_status, to_status, changed_at, "
            "changed_by) VALUES (:o, :u, :ord, :f, :t, :now, :by)"
        ),
        {"o": org_id, "u": user_id, "ord": order_id, "f": from_status, "t": to_status, "now": clock.now(),
         "by": changed_by},  # fmt: skip
    )


def _escape_like(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
