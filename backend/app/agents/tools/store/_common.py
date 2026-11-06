"""Store argument types. Args and org_of are shared by every vertical (common/args.py)."""

from decimal import Decimal
from typing import Annotated

from pydantic import Field, WithJsonSchema

from app.agents.tools.common.args import Args, org_of

__all__ = ["Address", "Args", "OrderNumber", "Price", "Sku", "org_of"]

# Prices are Decimal in code. The model sends a JSON number; pydantic parses it into a
# Decimal from its text form, so no float arithmetic ever happens.
Price = Annotated[Decimal, Field(ge=0, le=10_000_000, decimal_places=2), WithJsonSchema({"type": "number", "minimum": 0})]
Sku = Annotated[str, Field(min_length=1, max_length=40, description="Exact SKU from search_products, e.g. HT-GRN-001.")]
OrderNumber = Annotated[str, Field(min_length=1, max_length=30, description="The order number the customer sees, e.g. HT-1042.")]
Address = Annotated[str, Field(min_length=5, max_length=300)]

