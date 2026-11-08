"""search_products: the live catalogue (price and stock from the database, never the KB)."""

from pydantic import Field

from app.agents.tools.base import Tool, ToolResult, from_reply
from app.agents.tools.store._common import Args, Price, org_of
from app.db.session import tenant_tx
from app.domain.orders import service
from app.tenancy.context import ToolContext


class SearchProductsArgs(Args):
    query: str | None = Field(
        default=None, max_length=100, description="Words to match in the product name, e.g. 'oolong' or 'jasmine'."
    )
    category: str | None = Field(
        default=None,
        max_length=40,
        description="A category such as green, black, oolong, white, herbal, chai, teaware.",
    )
    min_price: Price | None = None
    max_price: Price | None = None


async def _handle(ctx: ToolContext, args: SearchProductsArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        org = await org_of(conn, ctx)
        reply = await service.search(conn, org, query=args.query, category=args.category,
                                     min_price=args.min_price, max_price=args.max_price)  # fmt: skip
    count = len(reply.cards[0]["products"]) if reply.cards else 0
    return from_reply(reply, f"Found {count} product{'s' if count != 1 else ''}")


search_products = Tool(
    name="search_products",
    live=True,
    description=(
        "Search the live product catalogue: returns SKU, name, category, current price and stock. "
        "Call it for any question about price or availability, and to find the SKU before create_order. "
        "All filters are optional; with none it lists products."
    ),
    args_model=SearchProductsArgs,
    handler=_handle,
    activity_label="Checking the catalogue…",
)
