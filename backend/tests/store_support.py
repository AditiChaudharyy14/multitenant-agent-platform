"""Helpers for the store tests: call tool handlers directly with a server-built ToolContext,
and read the database back as the owner (FORCE RLS applies, so it acts as the org admin).

A "turn" is just a fresh uuid here: the proposal rules compare the turn that created a
proposal with the turn that confirms it, nothing more.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.tools.base import Tool, ToolResult
from app.tenancy.context import ToolContext
from tests.factories import Shop, set_owner_tenant


def turn_ctx(shop: Shop, user: uuid.UUID, turn: uuid.UUID | None = None, conversation: uuid.UUID | None = None) -> ToolContext:
    return ToolContext(
        org_id=shop.org_id, user_id=user, role="customer",
        conversation_id=conversation or shop.conversations[user], turn_id=turn or uuid.uuid4(),
    )  # fmt: skip


async def call(tool: Tool, ctx: ToolContext, **args: Any) -> ToolResult:
    """Validate the arguments exactly like the loop does, then run the handler."""
    return await tool.handler(ctx, tool.args_model.model_validate(args))


def card(result: ToolResult, kind: str) -> dict[str, Any]:
    found = [c for c in result.cards if c["type"] == kind]
    assert len(found) == 1, f"expected one {kind} card, got {[c['type'] for c in result.cards]}: {result.content_for_model}"
    return found[0]


def proposal_id(result: ToolResult) -> uuid.UUID:
    return uuid.UUID(card(result, "order_summary")["proposal_id"])


async def scalar(owner: AsyncEngine, shop: Shop, sql: str, **params: Any) -> Any:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        return await conn.scalar(text(sql), {"o": shop.org_id, **params})


async def rows(owner: AsyncEngine, shop: Shop, sql: str, **params: Any) -> list[Any]:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        return list((await conn.execute(text(sql), {"o": shop.org_id, **params})).all())


async def stock(owner: AsyncEngine, shop: Shop, sku: str) -> int:
    return await scalar(owner, shop, "SELECT stock FROM products WHERE org_id = :o AND sku = :sku", sku=sku)


async def set_price(owner: AsyncEngine, shop: Shop, sku: str, price: str) -> None:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        await conn.execute(
            text("UPDATE products SET price = :p WHERE org_id = :o AND sku = :sku"),
            {"o": shop.org_id, "sku": sku, "p": Decimal(price)},
        )


async def set_stock(owner: AsyncEngine, shop: Shop, sku: str, qty: int) -> None:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        await conn.execute(
            text("UPDATE products SET stock = :q WHERE org_id = :o AND sku = :sku"),
            {"o": shop.org_id, "sku": sku, "q": qty},
        )


async def order_by_number(owner: AsyncEngine, shop: Shop, number: int) -> Any:
    found = await rows(owner, shop, "SELECT * FROM orders WHERE org_id = :o AND order_number = :n", n=number)
    return found[0] if found else None


async def order_items(owner: AsyncEngine, shop: Shop, number: int) -> dict[str, int]:
    found = await rows(
        owner, shop,
        "SELECT i.sku, i.qty FROM order_items i JOIN orders o ON o.id = i.order_id "
        "WHERE o.org_id = :o AND o.order_number = :n",
        n=number,
    )  # fmt: skip
    return dict(found)


async def history(owner: AsyncEngine, shop: Shop, number: int) -> list[tuple]:
    return await rows(
        owner, shop,
        "SELECT h.from_status, h.to_status, h.changed_by FROM order_status_history h "
        "JOIN orders o ON o.id = h.order_id WHERE o.org_id = :o AND o.order_number = :n "
        "ORDER BY h.changed_at, h.id",
        n=number,
    )  # fmt: skip


async def proposal_status(owner: AsyncEngine, shop: Shop, pid: uuid.UUID) -> str:
    return await scalar(owner, shop, "SELECT status FROM proposals WHERE org_id = :o AND id = :p", p=pid)


async def db_snapshot(owner: AsyncEngine, shop: Shop) -> dict[str, list]:
    """Everything an order tool could change. Equal snapshots mean nothing was written."""
    return {
        "orders": await rows(owner, shop, "SELECT id, order_number, status, delivery_address, total, updated_at "
                                          "FROM orders WHERE org_id = :o ORDER BY id"),
        "items": await rows(owner, shop, "SELECT id, sku, qty, unit_price FROM order_items WHERE org_id = :o ORDER BY id"),
        "history": await rows(owner, shop, "SELECT id FROM order_status_history WHERE org_id = :o ORDER BY id"),
        "stock": await rows(owner, shop, "SELECT sku, stock, price FROM products WHERE org_id = :o ORDER BY sku"),
        "proposals": await rows(owner, shop, "SELECT id, status FROM proposals WHERE org_id = :o ORDER BY id"),
    }  # fmt: skip
