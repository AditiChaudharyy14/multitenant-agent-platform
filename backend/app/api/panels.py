"""GET /api/orders and GET /api/bookings: the My orders / My bookings panel.

Built on the same domain functions as the list_my_orders / list_my_bookings tools
(orders.service.order_rows, bookings.service.booking_groups), so the panel and the agent
always agree. Identity comes only from the session. A panel the org's vertical doesn't
have answers with the same 404 as any missing resource (behaviour keys on vertical).
"""

from typing import Any

from fastapi import APIRouter, Depends

from app.api.conversations import not_found
from app.auth.deps import current_ctx
from app.db.session import tenant_tx
from app.domain.bookings import service as bookings
from app.domain.orders import service as orders
from app.tenancy.context import ToolContext
from app.tenancy.orgs import get_org_by_id

router = APIRouter(prefix="/api")


@router.get("/orders")
async def my_orders(ctx: ToolContext = Depends(current_ctx)) -> dict[str, Any]:
    async with tenant_tx(ctx.tenant) as conn:
        org = await get_org_by_id(conn, ctx.org_id)
        if org is None or org.vertical != "store":
            raise not_found()
        return {"orders": await orders.order_rows(conn, ctx, org)}


@router.get("/bookings")
async def my_bookings(ctx: ToolContext = Depends(current_ctx)) -> dict[str, Any]:
    async with tenant_tx(ctx.tenant) as conn:
        org = await get_org_by_id(conn, ctx.org_id)
        if org is None or org.vertical != "booking":
            raise not_found()
        return await bookings.booking_groups(conn, ctx, org)
