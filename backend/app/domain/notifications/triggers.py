"""The events that queue an email, each called inside the transaction that makes the change.

Recipients are read on the caller's connection: an admin-scoped one sees the customer's
row, and a customer-scoped one sees only themselves (RLS on users), which is exactly the
recipient there.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.money import format_money
from app.domain.notifications import outbox, templates
from app.domain.timefmt import local_display
from app.tenancy.orgs import Org


async def _recipient(conn: AsyncConnection, user_id: uuid.UUID) -> tuple[str, str]:
    row = (await conn.execute(text("SELECT email, display_name FROM users WHERE id = :u"), {"u": user_id})).one()
    return row.email, row.display_name


async def order_shipped(conn: AsyncConnection, org: Org, *, order_id: uuid.UUID, user_id: uuid.UUID,
                        number: str, address: str, total: Decimal) -> bool:  # fmt: skip
    items = [f"{r.qty} × {r.name}" for r in (await conn.execute(text(
        "SELECT name, qty FROM order_items WHERE order_id = :o ORDER BY name"), {"o": order_id})).all()]  # fmt: skip
    email, name = await _recipient(conn, user_id)
    mail = templates.order_shipped(org, name=name, number=number, items=items, address=address,
                                   total=format_money(total, org.currency))  # fmt: skip
    return await outbox.enqueue(conn, user_id=user_id, kind="order_shipped", to_email=email, subject=mail.subject,
                                body_text=mail.text, body_html=mail.html, dedupe_key=f"shipped:{order_id}")  # fmt: skip


async def _booking_mail(conn: AsyncConnection, org: Org, kind: str, *, booking_id: uuid.UUID, user_id: uuid.UUID,
                        number: str, service: str, practitioner: str, starts_at: datetime) -> bool:  # fmt: skip
    email, name = await _recipient(conn, user_id)
    build = templates.booking_confirmed if kind == "booking_confirmed" else templates.booking_reminder
    mail = build(org, name=name, number=number, service=service, practitioner=practitioner,
                 when=local_display(starts_at, org.timezone))  # fmt: skip
    # The start time is in the key: a rescheduled booking gets its own confirmation/reminder.
    key = f"{kind}:{booking_id}:{starts_at.isoformat()}"
    return await outbox.enqueue(conn, user_id=user_id, kind=kind, to_email=email, subject=mail.subject,
                                body_text=mail.text, body_html=mail.html, dedupe_key=key)  # fmt: skip


async def booking_confirmed(conn: AsyncConnection, org: Org, **booking: Any) -> bool:
    return await _booking_mail(conn, org, "booking_confirmed", **booking)


async def booking_reminder(conn: AsyncConnection, org: Org, **booking: Any) -> bool:
    return await _booking_mail(conn, org, "booking_reminder", **booking)
