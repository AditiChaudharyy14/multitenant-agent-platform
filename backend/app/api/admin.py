"""Admin-only order operations. A customer gets 403; a missing or foreign order gets the
same 404 as anything else that doesn't exist.

POST /api/admin/orders/{order_number}/advance moves an order one status on:
pending → confirmed → shipped → delivered (409 once it is delivered or cancelled).
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.conversations import not_found
from app.auth.deps import require_admin
from app.db.session import tenant_tx
from app.domain.orders.admin import CannotAdvance, OrderNotFound, advance
from app.domain.orders.numbers import display_number, parse_order_number
from app.tenancy.context import ToolContext
from app.tenancy.orgs import get_org_by_id

router = APIRouter(prefix="/api/admin")


class AdvanceOut(BaseModel):
    order_number: str
    from_status: str
    status: str


@router.post("/orders/{order_number}/advance")
async def advance_order(order_number: str, ctx: ToolContext = Depends(require_admin)) -> AdvanceOut:
    async with tenant_tx(ctx.tenant) as conn:
        org = await get_org_by_id(conn, ctx.org_id)
        number = parse_order_number(order_number, org.order_prefix or "") if org else None
        if number is None:
            raise not_found()
        try:
            before, after = await advance(conn, number)
        except OrderNotFound:
            raise not_found() from None
        except CannotAdvance as exc:
            raise HTTPException(status_code=409, detail=f"Order is {exc.status} and can't advance") from None
    return AdvanceOut(order_number=display_number(org.order_prefix, number), from_status=before, status=after)
