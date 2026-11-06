"""Conversation CRUD for the signed-in user. Identity comes only from the session.

A conversation that belongs to someone else (or another org), was deleted, or never existed
gets the same 404 body, so ids can't be probed.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.auth.deps import current_ctx
from app.db.session import tenant_tx
from app.domain.conversations import repo
from app.tenancy.context import ToolContext

router = APIRouter(prefix="/api/conversations")

NOT_FOUND = "Not found"


def not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=NOT_FOUND)


class ConversationOut(BaseModel):
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class RenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=repo.TITLE_MAX)]


@router.post("", status_code=201, response_model=ConversationOut)
async def create_conversation(ctx: ToolContext = Depends(current_ctx)) -> dict[str, Any]:
    async with tenant_tx(ctx.tenant) as conn:
        return await repo.create(conn, ctx)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(ctx: ToolContext = Depends(current_ctx)) -> list[dict[str, Any]]:
    async with tenant_tx(ctx.tenant) as conn:
        return await repo.list_own(conn, ctx)


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def rename_conversation(
    conversation_id: UUID, body: RenameIn, ctx: ToolContext = Depends(current_ctx)
) -> dict[str, Any]:
    async with tenant_tx(ctx.tenant) as conn:
        conv = await repo.rename(conn, ctx, conversation_id, body.title)
    if conv is None:
        raise not_found()
    return conv


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: UUID, ctx: ToolContext = Depends(current_ctx)) -> Response:
    async with tenant_tx(ctx.tenant) as conn:
        deleted = await repo.soft_delete(conn, ctx, conversation_id)
    if not deleted:
        raise not_found()
    return Response(status_code=204)


@router.get("/{conversation_id}/messages")
async def list_messages(conversation_id: UUID, ctx: ToolContext = Depends(current_ctx)) -> list[dict[str, Any]]:
    async with tenant_tx(ctx.tenant) as conn:
        messages = await repo.list_messages(conn, ctx, conversation_id)
    if messages is None:
        raise not_found()
    return messages
