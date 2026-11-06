"""Shared bits for the store tools: argument types and the org lookup."""

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema
from sqlalchemy.ext.asyncio import AsyncConnection

from app.tenancy.context import ToolContext
from app.tenancy.orgs import Org, get_org_by_id

# Prices are Decimal in code. The model sends a JSON number; pydantic parses it into a
# Decimal from its text form, so no float arithmetic ever happens.
Price = Annotated[Decimal, Field(ge=0, le=10_000_000, decimal_places=2), WithJsonSchema({"type": "number", "minimum": 0})]
Sku = Annotated[str, Field(min_length=1, max_length=40, description="Exact SKU from search_products, e.g. HT-GRN-001.")]
OrderNumber = Annotated[str, Field(min_length=1, max_length=30, description="The order number the customer sees, e.g. HT-1042.")]
Address = Annotated[str, Field(min_length=5, max_length=300)]


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")  # an unknown field (an org_id, say) is rejected


async def org_of(conn: AsyncConnection, ctx: ToolContext) -> Org:
    org = await get_org_by_id(conn, ctx.org_id)
    assert org is not None  # the session's org always exists (FK)
    return org
