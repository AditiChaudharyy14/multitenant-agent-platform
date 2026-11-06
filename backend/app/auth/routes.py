"""Sign-up, login, logout and /api/me.

The URL slug only chooses which org to sign up to or log in to. After that, identity
comes solely from the session (see deps.current_ctx).
"""

from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth.deps import current_ctx
from app.auth.passwords import DUMMY_HASH, hash_password, needs_rehash, verify_password
from app.auth.ratelimit import login_limiter
from app.auth.schemas import LoginIn, SignupIn
from app.auth.sessions import (
    COOKIE_NAME,
    clear_session_cookie,
    create_session,
    delete_session,
    set_session_cookie,
)
from app.db import session as db
from app.tenancy.context import ToolContext
from app.tenancy.orgs import Org, get_org_by_id, get_org_by_slug

router = APIRouter(prefix="/api")

# One body for every login failure (wrong org, unknown email, wrong password), so the
# response never reveals which part was wrong.
INVALID_LOGIN = "Invalid email or password"


async def _org_or_404(slug: str) -> Org:
    # orgs is global (no RLS) and public: it holds names and branding only.
    async with db.app_engine.connect() as conn:
        org = await get_org_by_slug(conn, slug)
    if org is None:
        raise HTTPException(status_code=404, detail="Not found")
    return org


def _user_out(user_id: UUID, email: str, display_name: str, role: str) -> dict[str, Any]:
    return {"id": user_id, "email": email, "display_name": display_name, "role": role}


@router.get("/orgs/{slug}")
async def public_org(slug: str) -> dict[str, Any]:
    """Branding for the login and sign-up pages."""
    return (await _org_or_404(slug)).public()


@router.post("/orgs/{slug}/auth/signup", status_code=201)
async def signup(slug: str, body: SignupIn, response: Response) -> dict[str, Any]:
    org = await _org_or_404(slug)
    password_hash = await hash_password(body.password)
    try:
        async with db.anon_tx(org.id) as conn:
            # Role is fixed here, never from the body; the users_admin_guard trigger also
            # rejects an admin insert from this anonymous context.
            user_id = await conn.scalar(
                text(
                    "INSERT INTO users (org_id, email, display_name, password_hash, role) "
                    "VALUES (:o, :e, :n, :h, 'customer') RETURNING id"
                ),
                {"o": org.id, "e": body.email, "n": body.display_name, "h": password_hash},
            )
            token = await create_session(conn, org.id, user_id, "customer")
    except IntegrityError as exc:
        if isinstance(exc.orig.__cause__, asyncpg.UniqueViolationError):
            raise HTTPException(status_code=409, detail="Email already registered") from None
        raise
    set_session_cookie(response, token)
    return {"user": _user_out(user_id, body.email, body.display_name, "customer"),
            "org": org.public()}  # fmt: skip


@router.post("/orgs/{slug}/auth/login")
async def login(slug: str, body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    # Rate limit first, so it also covers unknown slugs and emails.
    client_ip = request.client.host if request.client else "unknown"
    retry_after = login_limiter.hit(f"{client_ip}|{body.email}")
    if retry_after is not None:
        raise HTTPException(
            status_code=429, detail="Too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )  # fmt: skip

    org = await _org_or_404(slug)
    # The email is looked up only inside this org, so the same credentials registered
    # at another org simply aren't found here.
    async with db.anon_tx(org.id) as conn:
        user = (
            await conn.execute(
                text("SELECT id, email, display_name, role, password_hash FROM users WHERE email = :e"),
                {"e": body.email},
            )
        ).one_or_none()
        # Always run one argon2 verify, so a miss takes as long as a wrong password.
        ok = await verify_password(user.password_hash if user else DUMMY_HASH, body.password)
        if user is None or not ok:
            raise HTTPException(status_code=401, detail=INVALID_LOGIN)
        if needs_rehash(user.password_hash):
            await conn.execute(
                text("UPDATE users SET password_hash = :h WHERE id = :u"),
                {"h": await hash_password(body.password), "u": user.id},
            )
        token = await create_session(conn, org.id, user.id, user.role)
    set_session_cookie(response, token)
    return {"user": _user_out(user.id, user.email, user.display_name, user.role),
            "org": org.public()}  # fmt: skip


@router.post("/auth/logout", status_code=204)
async def logout(request: Request) -> Response:
    # Idempotent: with no or an unknown cookie there is simply nothing to delete.
    await delete_session(request.cookies.get(COOKIE_NAME, ""))
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.get("/me")
async def me(ctx: ToolContext = Depends(current_ctx)) -> dict[str, Any]:
    async with db.tenant_tx(ctx.tenant) as conn:
        user = (
            await conn.execute(
                text("SELECT id, email, display_name, role FROM users WHERE id = :u"),
                {"u": ctx.user_id},
            )
        ).one_or_none()
        org = await get_org_by_id(conn, ctx.org_id)
    if user is None or org is None:  # user deleted after the session was issued
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"user": _user_out(*user), "org": org.public()}
