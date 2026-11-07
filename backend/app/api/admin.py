"""Admin-only routes. Every route here depends on require_admin, so a customer gets 403
(tests/api/test_admin_routes.py walks this router to check that for every route); a
missing or foreign resource gets the same 404 as anything else that doesn't exist.

POST /api/admin/orders/{order_number}/advance moves an order one status on:
pending → confirmed → shipped → delivered (409 once it is delivered or cancelled).

The analytics chat: /api/admin/conversations[/{id}[/messages]] mirrors the customer chat
API for conversations of kind 'admin'. Its turns run the admin agent: the read-only metric
tools, which query as app_readonly in READ ONLY transactions. The chat itself (messages,
agent_runs) is stored as app_user, like any chat.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.agents.history import HISTORY_LIMIT, to_messages
from app.agents.providers.base import LLMProvider
from app.agents.turn import TurnInput
from app.api.chat import MAX_MESSAGE_CHARS, chat_limiter, get_chat_provider, stream_turn
from app.api.conversations import ConversationOut, RenameIn, not_found
from app.auth.deps import require_admin
from app.db.session import tenant_tx
from app.domain.conversations import repo as conversations
from app.domain.orders.admin import CannotAdvance, OrderNotFound, advance
from app.domain.orders.numbers import display_number, parse_order_number
from app.tenancy.context import ToolContext
from app.tenancy.orgs import get_org_by_id

router = APIRouter(prefix="/api/admin")
ADMIN = "admin"  # conversations.kind of the analytics chat


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


# ───────────── analytics chat ─────────────


class AdminMessageIn(BaseModel):
    """Typed questions only: the analytics chat has no card buttons."""

    model_config = ConfigDict(extra="forbid")
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS)]


@router.post("/conversations", status_code=201, response_model=ConversationOut)
async def create_admin_conversation(ctx: ToolContext = Depends(require_admin)) -> dict[str, Any]:
    async with tenant_tx(ctx.tenant) as conn:
        return await conversations.create(conn, ctx, ADMIN)


@router.get("/conversations", response_model=list[ConversationOut])
async def list_admin_conversations(ctx: ToolContext = Depends(require_admin)) -> list[dict[str, Any]]:
    async with tenant_tx(ctx.tenant) as conn:
        return await conversations.list_own(conn, ctx, ADMIN)


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
async def rename_admin_conversation(
    conversation_id: UUID, body: RenameIn, ctx: ToolContext = Depends(require_admin)
) -> dict[str, Any]:
    async with tenant_tx(ctx.tenant) as conn:
        conv = await conversations.rename(conn, ctx, conversation_id, body.title, ADMIN)
    if conv is None:
        raise not_found()
    return conv


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_admin_conversation(conversation_id: UUID, ctx: ToolContext = Depends(require_admin)) -> Response:
    async with tenant_tx(ctx.tenant) as conn:
        deleted = await conversations.soft_delete(conn, ctx, conversation_id, ADMIN)
    if not deleted:
        raise not_found()
    return Response(status_code=204)


@router.get("/conversations/{conversation_id}/messages")
async def list_admin_messages(conversation_id: UUID, ctx: ToolContext = Depends(require_admin)) -> list[dict[str, Any]]:
    async with tenant_tx(ctx.tenant) as conn:
        messages = await conversations.list_messages(conn, ctx, conversation_id, ADMIN)
    if messages is None:
        raise not_found()
    return messages


@router.post("/conversations/{conversation_id}/messages")
async def post_admin_message(
    conversation_id: UUID,
    body: AdminMessageIn,
    ctx: ToolContext = Depends(require_admin),
    provider: LLMProvider = Depends(get_chat_provider),
) -> StreamingResponse:
    retry_after = chat_limiter.hit(f"{ctx.org_id}:{ctx.user_id}")  # the same 20/min as the customer chat
    if retry_after is not None:
        raise HTTPException(status_code=429, detail="Too many messages, slow down a little",
                            headers={"Retry-After": str(retry_after)})  # fmt: skip
    async with tenant_tx(ctx.tenant) as conn:
        org = await get_org_by_id(conn, ctx.org_id)
        assert org is not None  # the session's org always exists (FK)
        history = to_messages(await conversations.recent_messages(conn, ctx, conversation_id, HISTORY_LIMIT))
        turn_id = await conversations.add_user_message(conn, ctx, conversation_id, body.content, ADMIN)
        if turn_id is None:
            raise not_found()  # not this admin's analytics chat: nothing was stored
    turn_ctx = ToolContext(ctx.org_id, ctx.user_id, ctx.role, conversation_id, turn_id)
    return stream_turn(provider, TurnInput(org=org, ctx=turn_ctx, history=history, user_text=body.content, agent=ADMIN))
