"""POST /api/conversations/{id}/messages: one chat turn, streamed as Server-Sent Events.

Events: token, tool_start, tool_end, card, citation, error, done. `done` always comes last
(also after an error), carrying the saved message id, status, cleaned content and
citations. A comment line (": heartbeat") goes out every 15 s of silence.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.agents.history import HISTORY_LIMIT, to_messages
from app.agents.providers import get_provider
from app.agents.providers.base import LLMProvider
from app.agents.turn import TurnInput, start_turn
from app.api.conversations import not_found
from app.auth.deps import current_ctx
from app.auth.ratelimit import SlidingWindowLimiter
from app.db.session import tenant_tx
from app.domain.conversations import repo
from app.tenancy.context import ToolContext
from app.tenancy.orgs import get_org_by_id

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


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS)]


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

    # One transaction: load history, store the user message (its id is the turn id), read the org.
    async with tenant_tx(ctx.tenant) as conn:
        history = to_messages(await repo.recent_messages(conn, ctx, conversation_id, HISTORY_LIMIT))
        turn_id = await repo.add_user_message(conn, ctx, conversation_id, body.content)
        if turn_id is None:
            raise not_found()  # rolls the transaction back; nothing was stored
        org = await get_org_by_id(conn, ctx.org_id)
    assert org is not None  # the session's org always exists (FK)

    turn_ctx = ToolContext(ctx.org_id, ctx.user_id, ctx.role, conversation_id, turn_id)
    inp = TurnInput(org=org, ctx=turn_ctx, history=history, user_text=body.content)
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
