"""POST /api/conversations/{id}/messages: one chat turn, streamed as Server-Sent Events.

Events: token, tool_start, tool_end, card, citation, error, done. `done` always comes last
(also after an error), carrying the saved message id, status, cleaned content and
citations. A comment line (": heartbeat") goes out every 15 s of silence.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from functools import lru_cache
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.agents.history import HISTORY_LIMIT, to_messages
from app.agents.providers import get_provider
from app.agents.providers.base import LLMProvider
from app.agents.tools.registry import tools_for
from app.agents.turn import ServerAction, TurnInput, start_turn
from app.api.conversations import not_found
from app.auth.deps import current_ctx
from app.auth.ratelimit import SlidingWindowLimiter
from app.db.session import tenant_tx
from app.domain.conversations import repo
from app.tenancy.context import ToolContext
from app.domain.slots.dates import parse_local_start
from app.domain.timefmt import local_display
from app.tenancy.orgs import Org, get_org_by_id

router = APIRouter(prefix="/api/conversations")

HEARTBEAT_SECONDS = 15.0
MAX_MESSAGE_CHARS = 4000

# 20 chat messages per minute per user (CLAUDE.md). Per process, like the login limiter.
chat_limiter = SlidingWindowLimiter(limit=20, window=timedelta(minutes=1))


@lru_cache
def get_chat_provider() -> LLMProvider:
    """One provider per process: its RPM limiters and router are shared by every request.
    Tests override this dependency with a FakeProvider."""
    return get_provider()


Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class ConfirmActionIn(BaseModel):
    """The Confirm button on a proposal card."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["confirm_proposal"]
    proposal_id: UUID

    def server_action(self) -> ServerAction:
        return ServerAction("confirm_proposal", {"proposal_id": str(self.proposal_id)}, "the Confirm button on a proposal")

    def describe(self, org: Org) -> str:
        return "Confirm"


class SelectSlotIn(BaseModel):
    """A time-slot button on a slot card. With a booking number it moves that booking;
    without, it starts a new one. Either way the tool only PROPOSES and re-validates
    every field, so a hand-crafted button can do no more than a typed message."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["select_slot"]
    service: Short
    start: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    practitioner: Short | None = None
    booking_number: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)] | None = None

    def server_action(self) -> ServerAction:
        if self.booking_number:
            return ServerAction("reschedule_booking", {"booking_number": self.booking_number, "new_start": self.start,
                                                       "practitioner": self.practitioner},
                                f"a time-slot button to move booking {self.booking_number}")  # fmt: skip
        return ServerAction("create_booking", {"service": self.service, "start": self.start, "practitioner": self.practitioner},
                            "a time-slot button")  # fmt: skip

    def describe(self, org: Org) -> str:
        """The stored user message: what the customer asked for, in words."""
        try:
            when = local_display(parse_local_start(self.start, org.timezone), org.timezone)
        except ValueError:
            when = self.start
        with_whom = f" with {self.practitioner}" if self.practitioner else ""
        if self.booking_number:
            return f"Move booking {self.booking_number} to {when}{with_whom}"
        return f"Book {self.service}, {when}{with_whom}"


# A card button; the type picks the model. Unknown types (or extra fields) are a 422.
ActionIn = Annotated[ConfirmActionIn | SelectSlotIn, Field(discriminator="type")]


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS)] | None = None
    action: ActionIn | None = None

    @model_validator(mode="after")
    def _content_or_action(self) -> "MessageIn":
        if self.content is None and self.action is None:
            raise ValueError("a message needs content or an action")
        return self

    def text(self, org: Org) -> str:
        """What is stored as the user's message (and shown to the model)."""
        if self.content is not None:
            return self.content
        assert self.action is not None  # the validator guarantees one of the two
        return self.action.describe(org)


@router.post("/{conversation_id}/messages")
async def post_message(
    conversation_id: UUID,
    body: MessageIn,
    ctx: ToolContext = Depends(current_ctx),
    provider: LLMProvider = Depends(get_chat_provider),
) -> StreamingResponse:
    retry_after = chat_limiter.hit(f"{ctx.org_id}:{ctx.user_id}")
    if retry_after is not None:
        raise HTTPException(
            status_code=429, detail="Too many messages, slow down a little",
            headers={"Retry-After": str(retry_after)},
        )  # fmt: skip

    action = body.action.server_action() if body.action else None
    # One transaction: read the org, load history, store the user message (its id is the turn id).
    async with tenant_tx(ctx.tenant) as conn:
        org = await get_org_by_id(conn, ctx.org_id)
        assert org is not None  # the session's org always exists (FK)
        # A button runs a tool of the org's own vertical (keyed on vertical, never slug).
        if action is not None and action.tool not in tools_for(org.vertical):
            raise HTTPException(status_code=422, detail="This action isn't available here")
        history = to_messages(await repo.recent_messages(conn, ctx, conversation_id, HISTORY_LIMIT))
        user_text = body.text(org)
        turn_id = await repo.add_user_message(conn, ctx, conversation_id, user_text)
        if turn_id is None:
            raise not_found()  # rolls the transaction back; nothing was stored

    turn_ctx = ToolContext(ctx.org_id, ctx.user_id, ctx.role, conversation_id, turn_id)
    inp = TurnInput(org=org, ctx=turn_ctx, history=history, user_text=user_text, action=action)
    return stream_turn(provider, inp)


def stream_turn(provider: LLMProvider, inp: TurnInput) -> StreamingResponse:
    """The SSE response for one turn (customer or admin chat)."""
    return StreamingResponse(
        _stream(provider, inp),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream(provider: LLMProvider, inp: TurnInput) -> AsyncIterator[str]:
    # The turn starts only once the response does, so it is never left running unwatched.
    task, queue = start_turn(provider, inp)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": heartbeat\n\n"  # keeps proxies from closing an idle stream
                continue
            if event is None:
                return
            yield event.sse()
    finally:
        # Client disconnect or Stop: Starlette cancels this generator. Cancel the turn too;
        # it persists its partial answer as 'stopped' on its own. Not awaited: awaiting
        # inside a cancelled scope would be cancelled again at once.
        if not task.done():
            task.cancel()
