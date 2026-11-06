"""Admin (and demo) order transitions: pending → confirmed → shipped → delivered.

Runs in an admin-scoped tenant_tx (RLS still limits it to the org). Delivered and
cancelled orders are terminal. Every step writes order_status_history (changed_by admin).
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.orders import repo
from app.domain.orders.repo import Order

NEXT_STATUS = {"pending": "confirmed", "confirmed": "shipped", "shipped": "delivered"}


class OrderNotFound(Exception):
    pass


class CannotAdvance(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(f"order is {status}, which is final; it can't advance")
        self.status = status


async def advance(conn: AsyncConnection, order_number: int) -> tuple[str, str]:
    """Move one order one step on. Returns (from_status, to_status)."""
    row = (
        await conn.execute(
            text(
                "SELECT id, user_id, order_number, status, delivery_address, total, created_at FROM orders "
                "WHERE org_id = app_org() AND order_number = :n FOR UPDATE"
            ),
            {"n": order_number},
        )
    ).one_or_none()
    if row is None:
        raise OrderNotFound(str(order_number))
    order = Order(*row)
    to_status = NEXT_STATUS.get(order.status)
    if to_status is None:
        raise CannotAdvance(order.status)
    org_id = await conn.scalar(text("SELECT app_org()"))
    await repo.set_status(conn, org_id, order, to_status, "admin")
    return order.status, to_status
