"""The outbox row itself. Idempotent: (org_id, dedupe_key) is unique, a repeat is a no-op."""

import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app import clock

UNIQUE_VIOLATION = "23505"


async def enqueue(conn: AsyncConnection, *, user_id: uuid.UUID, kind: str, to_email: str, subject: str,
                  body_text: str, body_html: str, dedupe_key: str) -> bool:  # fmt: skip
    """Queue one email in the caller's transaction. True if queued, False if that dedupe
    key was already queued.

    A plain INSERT in a savepoint, not ON CONFLICT or RETURNING: both need the SELECT
    policy, and a customer-scoped transaction (a booking the customer confirms) may insert
    its own row but never read the outbox (RLS, migration 0009)."""
    try:
        async with conn.begin_nested():
            await conn.execute(text(
                "INSERT INTO email_outbox (org_id, user_id, kind, to_email, subject, body_text, body_html, dedupe_key, "
                "next_attempt_at, created_at) VALUES (app_org(), :u, :k, :to, :s, :t, :h, :d, :now, :now)"),
                {"u": user_id, "k": kind, "to": to_email, "s": subject, "t": body_text, "h": body_html, "d": dedupe_key,
                 "now": clock.now()})  # fmt: skip
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == UNIQUE_VIOLATION or "email_outbox_org_id_dedupe_key_key" in str(exc):
            return False  # already queued: the savepoint rolled back only this insert
        raise
    return True
