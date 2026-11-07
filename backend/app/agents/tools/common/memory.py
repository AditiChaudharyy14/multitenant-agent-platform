"""Memory tools, shared by every vertical (what is derived still depends on the vertical).

Identity never appears in the arguments: whose memory it is comes only from the ToolContext,
and RLS (own_rows) backs that up. These write the customer's own preferences directly, not
through a proposal: a memory is not one of the mutating business actions of invariant 6
(no order, booking, stock or money changes), and the customer can delete it at any time.
"""

import json

from pydantic import Field

from app.agents.tools.base import Tool, ToolResult, error_result
from app.agents.tools.common.args import Args, NoArgs, org_of
from app.db.session import tenant_tx
from app.domain import memory
from app.tenancy.context import ToolContext


class RememberArgs(Args):
    key: str = Field(description="A short snake_case name for the preference, e.g. preferred_practitioner, milk, spice_level.")
    value: str = Field(description="What to remember, in a few words, e.g. 'Maya Tamang' or 'oat milk'. Never a price or stock figure.")


class ForgetArgs(Args):
    key: str = Field(description="The key to forget, exactly as get_my_preferences lists it.")


async def _remember(ctx: ToolContext, args: RememberArgs) -> ToolResult:
    try:
        async with tenant_tx(ctx.tenant) as conn:
            m = await memory.remember(conn, ctx, args.key, args.value)
    except memory.MemoryRefused as exc:
        return error_result(str(exc))
    return ToolResult(json.dumps({"remembered": m.as_dict()}, ensure_ascii=False), activity_label="Noted")


async def _forget(ctx: ToolContext, args: ForgetArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        done = await memory.forget(conn, ctx, args.key)
    if not done:
        return ToolResult(json.dumps({"forgotten": False, "reason": "no such memory"}), activity_label="Nothing to forget")
    return ToolResult(json.dumps({"forgotten": True, "key": memory.normalise_key(args.key)}), activity_label="Forgotten")


async def _get(ctx: ToolContext, args: NoArgs) -> ToolResult:
    async with tenant_tx(ctx.tenant) as conn:
        org = await org_of(conn, ctx)
        found = await memory.list_memories(conn, ctx, org.vertical)
    return ToolResult(json.dumps({"memories": [m.as_dict() for m in found]}, ensure_ascii=False))


remember_preference = Tool(
    name="remember_preference",
    description="Save one of the customer's own preferences so you remember it in future conversations. "
    "Call it only when the customer asks you to remember something (or clearly states a lasting preference).",
    args_model=RememberArgs,
    handler=_remember,
    activity_label="Remembering…",
)

forget_preference = Tool(
    name="forget_preference",
    description="Forget one of the customer's saved preferences or a fact derived from their history "
    "(e.g. usual_products), when they ask you to.",
    args_model=ForgetArgs,
    handler=_forget,
    activity_label="Forgetting…",
)

get_my_preferences = Tool(
    name="get_my_preferences",
    description="Everything you remember about this customer: preferences they asked you to save (stated) "
    "and facts from their own history (derived), e.g. their usual products, practitioner or service.",
    args_model=NoArgs,
    handler=_get,
    activity_label="Checking what I remember…",
)

MEMORY_TOOLS = [remember_preference, forget_preference, get_my_preferences]
