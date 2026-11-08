"""Server-side sessions. The cookie holds a random token; the database holds only its
SHA-256, plus the org, user and role it stands for.

The sessions table has no RLS: it is found only by token hash, before the org is known.
"""

import hashlib
import secrets
from datetime import timedelta
from uuid import UUID

from fastapi import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app import clock
from app.config import get_settings
from app.db import session as db

COOKIE_NAME = "sid"
SESSION_TTL = timedelta(days=7)
# token_urlsafe(32) is 43 characters; anything far longer is junk, not worth hashing.
_MAX_TOKEN_LEN = 128


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def create_session(conn: AsyncConnection, org_id: UUID, user_id: UUID, role: str) -> str:
    """Insert a session inside the caller's transaction and return the raw token."""
    token = secrets.token_urlsafe(32)  # fresh on every login: no session fixation
    now = clock.now()
    await conn.execute(
        text(
            "INSERT INTO sessions (token_hash, org_id, user_id, role, created_at, expires_at) "
            "VALUES (:h, :o, :u, :r, :now, :exp)"
        ),
        {"h": token_hash(token), "o": org_id, "u": user_id, "r": role, "now": now,
         "exp": now + SESSION_TTL},
    )  # fmt: skip
    return token


async def lookup_session(token: str) -> tuple[UUID, UUID, str] | None:
    """(org_id, user_id, role) for a live session, else None. Reads only `sessions`."""
    if not token or len(token) > _MAX_TOKEN_LEN:
        return None
    async with db.app_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT org_id, user_id, role FROM sessions " "WHERE token_hash = :h AND expires_at > :now"),
                {"h": token_hash(token), "now": clock.now()},
            )
        ).one_or_none()
    return (row.org_id, row.user_id, row.role) if row else None


async def delete_session(token: str) -> None:
    if not token or len(token) > _MAX_TOKEN_LEN:
        return
    async with db.app_engine.begin() as conn:
        await conn.execute(text("DELETE FROM sessions WHERE token_hash = :h"), {"h": token_hash(token)})


def _cookie_flags() -> dict:
    # Secure everywhere except plain-HTTP local development.
    return {"httponly": True, "samesite": "lax", "path": "/",
            "secure": get_settings().app_env != "local"}  # fmt: skip


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=int(SESSION_TTL.total_seconds()), **_cookie_flags())


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, **_cookie_flags())
