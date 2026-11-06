"""confirm_proposal: the model's way to commit a proposal after the customer said yes.

All rules live in app/domain/proposals/service.py: the server refuses a proposal made in
this same turn, an expired or replaced one, or one that isn't the caller's. The Confirm
button reaches the same service without any model involved (app/agents/turn.py).
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.agents.tools.base import Tool, ToolResult, from_reply
from app.domain.proposals import service
from app.tenancy.context import ToolContext

_LABELS = {"committed": "Done", "refused": "Couldn't confirm", "changed": "Something changed", "not_found": "Couldn't confirm"}


class ConfirmProposalArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=64, description="The proposal_id from the summary you showed.")


async def _handle(ctx: ToolContext, args: ConfirmProposalArgs) -> ToolResult:
    try:
        pid = uuid.UUID(args.proposal_id)
    except ValueError:
        return from_reply(service.NOT_FOUND, _LABELS["not_found"])
    reply = await service.confirm(ctx, pid)
    return from_reply(reply, _LABELS.get(reply.status, "Done"))


confirm_proposal = Tool(
    name="confirm_proposal",
    description=(
        "Commit a proposal the customer has explicitly accepted in their latest message ('yes', "
        "'go ahead', 'confirm'). Never call it in the same turn you made the proposal, and never "
        "without a clear yes. If it reports CHANGED, show the fresh summary and ask again."
    ),
    args_model=ConfirmProposalArgs,
    handler=_handle,
    activity_label="Confirming…",
)
