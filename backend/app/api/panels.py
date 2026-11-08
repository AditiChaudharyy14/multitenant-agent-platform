"""GET /api/orders and GET /api/bookings: the My orders / My bookings panel, plus
GET/DELETE /api/memories for its "What I remember" section.

Built on the same domain functions as the list_my_orders / list_my_bookings tools
(orders.service.order_rows, bookings.service.booking_groups), so the panel and the agent
always agree. Identity comes only from the session. A panel the org's vertical doesn't
have answers with the same 404 as any missing resource (behaviour keys on vertical).
"""

from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.conversations import not_found
from app.auth.deps import current_ctx
from app.db.session import tenant_tx
from app.domain import memory
from app.domain.bookings import service as bookings
from app.domain.orders import service as orders
from app.tenancy.context import ToolContext
from app.tenancy.orgs import Org, get_org_by_id

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


# ───────────── "What I remember" ─────────────
# The same domain functions as the memory tools (domain.memory), so the panel lists exactly
# what the agent knows. Off for an org whose `features.memory` is false: the same 404.


async def _memory_org(conn: AsyncConnection, ctx: ToolContext) -> Org:
    org = await get_org_by_id(conn, ctx.org_id)
    if org is None or ctx.role != "customer" or not org.has("memory"):
        raise not_found()
    return org


@router.get("/memories")
async def my_memories(ctx: ToolContext = Depends(current_ctx)) -> dict[str, Any]:
    async with tenant_tx(ctx.tenant) as conn:
        org = await _memory_org(conn, ctx)
        return {"memories": [m.as_dict() for m in await memory.list_memories(conn, ctx, org.vertical)]}


@router.delete("/memories/{key}", status_code=204)
async def forget_memory(key: str, ctx: ToolContext = Depends(current_ctx)) -> Response:
    async with tenant_tx(ctx.tenant) as conn:
        await _memory_org(conn, ctx)
        if not await memory.forget(conn, ctx, key):  # missing, or someone else's: same 404
            raise not_found()
    return Response(status_code=204)
