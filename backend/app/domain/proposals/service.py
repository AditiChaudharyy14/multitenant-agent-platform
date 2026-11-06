"""confirm(): the only way a proposal becomes a real change. Vertical-agnostic.

Rules, all checked inside one transaction with the proposal row locked:
  - it must be the caller's, in this conversation (otherwise: not found);
  - it must be open (confirmed, superseded, expired or rejected ones are refused, so a
    double confirm never commits twice);
  - it must not have expired (15 minutes);
  - it must not have been created in the current turn: the customer has to answer in a
    new message first, so the model can't propose and confirm in one breath.
Then the committer for its kind re-checks live facts and either commits, refuses, or
reports a change (and has issued a fresh proposal). Only a commit marks it 'confirmed'.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncConnection

from app import clock
from app.db.session import tenant_tx
from app.domain.proposals import repo
from app.domain.proposals.repo import PROPOSAL_TTL, Proposal
from app.domain.reply import Reply
from app.tenancy.context import ToolContext

__all__ = ["PROPOSAL_TTL", "CommitAborted", "Committer", "confirm"]

log = logging.getLogger("app.proposals")

# A committer gets the locked proposal inside the confirm transaction. Its Reply.status
# is 'committed', 'refused' or 'changed'. A commit failure must raise, rolling back all.
Committer = Callable[[AsyncConnection, ToolContext, Proposal], Awaitable[Reply]]

NOT_FOUND = Reply(
    "NOT FOUND: there is no such proposal for this customer in this conversation. "
    "Prepare a new one if they still want to go ahead.",
    status="not_found",
)
_NOT_OPEN = {
    "confirmed": "this proposal was already confirmed; nothing new was placed",
    "superseded": "this proposal was superseded by a newer one; use the latest summary",
    "expired": "this proposal expired (they last 15 minutes); offer to prepare a fresh one",
    "rejected": "this proposal was rejected earlier and can no longer be carried out",
}
SAME_TURN = (
    "REFUSED: the customer has not confirmed yet. Show them the summary and wait for "
    "their reply in a new message; never confirm in the turn that made the proposal."
)


def _committers() -> dict[str, Committer]:
    # Imported here, not at module level: the committers themselves import the proposal
    # repo to issue fresh proposals, and this keeps the import graph acyclic.
    from app.domain.orders.commit import COMMITTERS as ORDER_COMMITTERS

    return {**ORDER_COMMITTERS}  # booking committers join here


async def confirm(ctx: ToolContext, proposal_id: uuid.UUID) -> Reply:
    try:
        async with tenant_tx(ctx.tenant) as conn:
            return await _confirm(conn, ctx, proposal_id)
    except CommitAborted as abort:  # caught outside the block: the transaction rolled back
        return abort.reply


class CommitAborted(Exception):
    """Raised by a committer to roll the whole confirm transaction back (e.g. a stock
    decrement that matched no row), answering with `reply`. Nothing is committed, not
    even the proposal's status."""

    def __init__(self, reply: Reply) -> None:
        super().__init__(reply.text)
        self.reply = reply


async def _confirm(conn: AsyncConnection, ctx: ToolContext, proposal_id: uuid.UUID) -> Reply:
    proposal = await repo.lock(conn, ctx, proposal_id)
    if proposal is None:
        return NOT_FOUND
    if proposal.status != "open":
        return Reply(f"REFUSED: {_NOT_OPEN[proposal.status]}.", status="refused")
    if proposal.expires_at <= clock.now():
        await repo.set_status(conn, proposal.id, "expired")  # committed: the refusal sticks
        return Reply(f"REFUSED: {_NOT_OPEN['expired']}.", status="refused")
    if proposal.created_turn_id == ctx.turn_id:
        return Reply(SAME_TURN, status="refused")

    committer = _committers().get(proposal.kind)
    if committer is None:  # a kind with no committer is a bug; never commit by guesswork
        log.error("no committer for proposal kind %r", proposal.kind)
        raise CommitAborted(Reply("REFUSED: this kind of request can't be confirmed.", status="refused"))

    reply = await committer(conn, ctx, proposal)
    if reply.status == "committed":
        await repo.set_status(conn, proposal.id, "confirmed")
    elif reply.status == "changed":
        # Usually already superseded by the fresh proposal; this covers "no fresh one".
        await repo.set_status(conn, proposal.id, "superseded")
    else:
        await repo.set_status(conn, proposal.id, "rejected")
    return reply
