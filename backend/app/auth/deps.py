"""Request identity. The ONLY source is the server-side session behind the `sid` cookie:
org_id, user_id and role are never read from the URL, headers, query or body.
"""

from fastapi import Depends, HTTPException, Request

from app.auth.sessions import COOKIE_NAME, lookup_session
from app.tenancy.context import ToolContext


async def current_ctx(request: Request) -> ToolContext:
    found = await lookup_session(request.cookies.get(COOKIE_NAME, ""))
    if found is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    org_id, user_id, role = found
    return ToolContext(org_id=org_id, user_id=user_id, role=role)


async def require_admin(ctx: ToolContext = Depends(current_ctx)) -> ToolContext:
    if ctx.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    return ctx
