"""Shared bits for every vertical's tools: the strict argument base and the org lookup."""

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection

from app.tenancy.context import ToolContext
from app.tenancy.orgs import Org, get_org_by_id


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")  # an unknown field (an org_id, say) is rejected


class NoArgs(Args):
    pass


async def org_of(conn: AsyncConnection, ctx: ToolContext) -> Org:
    org = await get_org_by_id(conn, ctx.org_id)
    assert org is not None  # the session's org always exists (FK)
    return org
