"""Request identity. The ONLY source is the server-side session behind the `sid` cookie:
org_id, user_id and role are never read from the URL, headers, query or body.
"""

from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text

from app.auth.sessions import COOKIE_NAME, lookup_session
from app.db import session as db
from app.logging_setup import set_org
from app.tenancy.context import ToolContext


async def current_ctx(request: Request) -> ToolContext:
    found = await lookup_session(request.cookies.get(COOKIE_NAME, ""))
    if found is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    org_id, user_id, role = found
    set_org(await _slug_of(org_id))  # for the structured logs (app/logging_setup.py)
    return ToolContext(org_id=org_id, user_id=user_id, role=role)


_slugs: dict[UUID, str] = {}  # org id -> slug; orgs change only with make sync-orgs


async def _slug_of(org_id: UUID) -> str | None:
    if org_id not in _slugs:
        async with db.app_engine.connect() as conn:  # orgs is global: app_user may read it
            slug = await conn.scalar(text("SELECT slug FROM orgs WHERE id = :o"), {"o": org_id})
        if slug is None:
            return None
        _slugs[org_id] = slug
    return _slugs[org_id]


async def require_admin(ctx: ToolContext = Depends(current_ctx)) -> ToolContext:
    if ctx.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    return ctx
