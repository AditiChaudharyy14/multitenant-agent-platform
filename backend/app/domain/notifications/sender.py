"""Deliver pending outbox rows over SMTP and mark them sent; back off on failure.

Per org, admin-scoped, as app_user. Rows are claimed with FOR UPDATE SKIP LOCKED and the
transaction stays open while they are sent (at most BATCH, to a local SMTP server), so two
senders never deliver the same row. smtplib is blocking, so it runs in a thread.
"""

import asyncio
import logging
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from sqlalchemy import text

from app import clock
from app.config import get_settings
from app.db.session import TenantContext, app_engine, tenant_tx
from app.tenancy.orgs import get_org_by_id

log = logging.getLogger("app.email")

BATCH = 20
MAX_ATTEMPTS = 5  # then the row is marked failed and left for a human to look at


def _send(host: str, port: int, msg: EmailMessage) -> None:
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        smtp.send_message(msg)


def _message(sender_name: str, row: Any) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, get_settings().email_from))
    msg["To"] = row.to_email
    msg["Subject"] = row.subject
    msg.set_content(row.body_text)
    msg.add_alternative(row.body_html, subtype="html")
    return msg


async def run_once(*, host: str | None = None, port: int | None = None) -> int:
    """Send what is due in every org. Returns how many were delivered."""
    s = get_settings()
    host, port = host or s.smtp_host, port or s.smtp_port
    async with app_engine.connect() as conn:  # orgs is global: app_user may read it
        org_ids = list(await conn.scalars(text("SELECT id FROM orgs ORDER BY slug")))
    sent = 0
    for org_id in org_ids:
        async with tenant_tx(TenantContext(org_id=org_id, user_id=None, role="admin")) as conn:
            now = clock.now()
            rows = (await conn.execute(text(
                "SELECT id, to_email, subject, body_text, body_html, attempts FROM email_outbox "
                "WHERE status = 'pending' AND next_attempt_at <= :now ORDER BY created_at LIMIT :n FOR UPDATE SKIP LOCKED"),
                {"now": now, "n": BATCH})).all()  # fmt: skip
            if not rows:
                continue
            org = await get_org_by_id(conn, org_id)
            assert org is not None
            for row in rows:
                try:
                    await asyncio.to_thread(_send, host, port, _message(org.name, row))
                except (OSError, smtplib.SMTPException) as exc:
                    attempts = row.attempts + 1
                    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                    log.warning(
                        "email %s to org %s failed (attempt %d): %s", row.id, org.slug, attempts, type(exc).__name__
                    )
                    await conn.execute(text(
                        "UPDATE email_outbox SET attempts = :a, status = :s, last_error = :e, next_attempt_at = :next "
                        "WHERE id = :i"), {"a": attempts, "s": status, "e": f"{type(exc).__name__}: {exc}"[:500],
                                           "next": now + timedelta(minutes=2**attempts), "i": row.id})  # fmt: skip
                    continue
                await conn.execute(text("UPDATE email_outbox SET status = 'sent', sent_at = :now, attempts = :a WHERE id = :i"),
                                   {"now": now, "a": row.attempts + 1, "i": row.id})  # fmt: skip
                sent += 1
    return sent
