"""Email notifications through a transactional outbox (no real email: Mailpit locally).

- Triggers write an outbox row in the SAME transaction as the change: an order advanced to
  shipped, a booking confirmed, and (scheduler) a reminder 24 h before a booking.
- Every row has a dedupe key, so repeating a trigger never queues a second email; the
  reminder job is idempotent through bookings.reminder_sent_at as well.
- The sender delivers pending rows over SMTP and marks them sent, backing off on failure.
- The outbox is per-org (RLS) and customers can't read it.
All as app_user.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from email import message_from_bytes
from email.message import Message

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock
from app.agents.tools.booking import cancel_booking, create_booking, reschedule_booking
from app.agents.tools.common.confirm_proposal import confirm_proposal
from app.db.session import TenantContext, tenant_tx
from app.domain.notifications import outbox, reminders, sender
from app.domain.orders.admin import advance
from tests import booking_support as bs
from tests.factories import Shop, Studio, insert_shop_order, set_owner_tenant

pytestmark = pytest.mark.usefixtures("as_app_user")

TUE_14 = bs.local(2026, 11, 10, 14, 0)


async def outbox_rows(owner: AsyncEngine, org_id: uuid.UUID) -> list:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, org_id)
        return list((await conn.execute(text(
            "SELECT kind, to_email, subject, body_text, body_html, dedupe_key, status, attempts, next_attempt_at, sent_at "
            "FROM email_outbox ORDER BY created_at"))).all())  # fmt: skip


async def email_of(owner: AsyncEngine, org_id: uuid.UUID, user: uuid.UUID) -> str:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, org_id)
        return await conn.scalar(text("SELECT email FROM users WHERE id = :u"), {"u": user})


async def admin_advance(shop: Shop, number: int) -> tuple[str, str]:
    async with tenant_tx(TenantContext(shop.org_id, shop.admin, "admin")) as conn:
        return await advance(conn, number)


async def book(studio: Studio, start=TUE_14, user=None) -> int:  # noqa: ANN001
    user = user or studio.x
    proposed = await bs.call(create_booking, bs.turn_ctx(studio, user), service="swedish", start=bs.iso(start))
    done = await bs.call(confirm_proposal, bs.turn_ctx(studio, user), proposal_id=bs.proposal_id(proposed))
    assert done.ok and "CONFIRMED" in done.content_for_model, done.content_for_model
    return int(bs.card(done, "booking_confirmed")["booking_number"].rsplit("-", 1)[1])


# ───────────── order shipped ─────────────


async def test_advancing_to_shipped_queues_one_branded_email(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 2)], status="confirmed")
    assert await admin_advance(shop, n) == ("confirmed", "shipped")
    rows = await outbox_rows(owner_engine, shop.org_id)
    assert [r.kind for r in rows] == ["order_shipped"]
    r = rows[0]
    assert r.to_email == await email_of(owner_engine, shop.org_id, shop.x)
    assert r.subject.startswith(f"Your order {shop.number(n)} has shipped")
    assert "Test shop" in r.body_text and "Green Alpha" in r.body_text and "cash on delivery" in r.body_text.lower()
    assert "Test shop" in r.body_html and r.status == "pending"
    assert await admin_advance(shop, n) == ("shipped", "delivered")  # delivered: no email
    assert len(await outbox_rows(owner_engine, shop.org_id)) == 1


async def test_only_shipped_sends_an_email(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)])  # pending
    assert await admin_advance(shop, n) == ("pending", "confirmed")
    assert await outbox_rows(owner_engine, shop.org_id) == []


async def test_the_same_trigger_twice_queues_one_email(shop: Shop, owner_engine: AsyncEngine) -> None:
    async with tenant_tx(TenantContext(shop.org_id, shop.admin, "admin")) as conn:
        for _ in range(2):
            await outbox.enqueue(conn, user_id=shop.x, kind="order_shipped", to_email="x@example.test",
                                 subject="s", body_text="t", body_html="<p>t</p>", dedupe_key="shipped:same")  # fmt: skip
    assert len(await outbox_rows(owner_engine, shop.org_id)) == 1


# ───────────── booking confirmed ─────────────


async def test_confirming_a_booking_queues_a_confirmation(studio: Studio, owner_engine: AsyncEngine) -> None:
    with clock.freeze(bs.NOW):
        n = await book(studio)
    rows = await outbox_rows(owner_engine, studio.org_id)
    assert [r.kind for r in rows] == ["booking_confirmed"]
    r = rows[0]
    assert r.to_email == await email_of(owner_engine, studio.org_id, studio.x)
    assert studio.number(n) in r.subject and "Swedish Massage" in r.subject
    assert "Tue 10 Nov 2026" in r.body_text and "14:00" in r.body_text  # org-local, absolute date
    assert "Test studio" in r.body_text or "Test studio" in r.body_html


# ───────────── reminders ─────────────


async def test_reminder_is_queued_once_inside_24h(studio: Studio, owner_engine: AsyncEngine) -> None:
    with clock.freeze(bs.NOW):
        n = await book(studio)
    with clock.freeze(TUE_14 - timedelta(hours=30)):  # too early
        assert await reminders.run_once() == 0
    with clock.freeze(TUE_14 - timedelta(hours=23)):
        assert await reminders.run_once() >= 1
        assert await reminders.run_once() == 0  # idempotent: reminder_sent_at is set
    rows = [r for r in await outbox_rows(owner_engine, studio.org_id) if r.kind == "booking_reminder"]
    assert len(rows) == 1 and studio.number(n) in rows[0].subject
    assert "Reminder" in rows[0].subject


async def test_rescheduling_resets_the_reminder(studio: Studio, owner_engine: AsyncEngine) -> None:
    with clock.freeze(bs.NOW):
        n = await book(studio)
    with clock.freeze(TUE_14 - timedelta(hours=20)):
        await reminders.run_once()
        proposed = await bs.call(reschedule_booking, bs.turn_ctx(studio, studio.x), booking_number=studio.number(n),
                                 new_start=bs.iso(bs.local(2026, 11, 12, 10, 0)))  # fmt: skip
        done = await bs.call(confirm_proposal, bs.turn_ctx(studio, studio.x), proposal_id=bs.proposal_id(proposed))
        assert done.ok and "CONFIRMED" in done.content_for_model, done.content_for_model
    with clock.freeze(bs.local(2026, 11, 11, 12, 0)):  # 22 h before the new time
        await reminders.run_once()
    rows = [r for r in await outbox_rows(owner_engine, studio.org_id) if r.kind == "booking_reminder"]
    assert len(rows) == 2 and rows[0].dedupe_key != rows[1].dedupe_key
    assert "Thu 12 Nov 2026" in rows[1].body_text


async def test_a_cancelled_booking_gets_no_reminder(studio: Studio, owner_engine: AsyncEngine) -> None:
    with clock.freeze(bs.NOW):
        n = await book(studio)
        proposed = await bs.call(cancel_booking, bs.turn_ctx(studio, studio.x), booking_number=studio.number(n))
        assert (await bs.call(confirm_proposal, bs.turn_ctx(studio, studio.x), proposal_id=bs.proposal_id(proposed))).ok
    with clock.freeze(TUE_14 - timedelta(hours=10)):
        await reminders.run_once()
    assert not [r for r in await outbox_rows(owner_engine, studio.org_id) if r.kind == "booking_reminder"]


# ───────────── isolation ─────────────


async def test_customers_cannot_read_the_outbox_and_orgs_are_separate(
    shop: Shop, other_shop: Shop, owner_engine: AsyncEngine
) -> None:  # fmt: skip
    n = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status="confirmed")
    await admin_advance(shop, n)
    async with tenant_tx(TenantContext(shop.org_id, shop.x, "customer")) as conn:
        assert await conn.scalar(text("SELECT count(*) FROM email_outbox")) == 0  # even their own email
    async with tenant_tx(TenantContext(shop.org_id, shop.admin, "admin")) as conn:
        assert await conn.scalar(text("SELECT count(*) FROM email_outbox")) == 1
    async with tenant_tx(TenantContext(other_shop.org_id, other_shop.admin, "admin")) as conn:
        assert await conn.scalar(text("SELECT count(*) FROM email_outbox")) == 0


# ───────────── sender ─────────────


@contextlib.asynccontextmanager
async def smtp_stub(fail: bool = False) -> AsyncIterator[tuple[int, list[Message]]]:
    """A tiny SMTP server on localhost (stdlib smtpd is gone in 3.12): enough of the protocol
    for smtplib's send_message. fail=True answers MAIL FROM with 451."""
    received: list[Message] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        def say(line: str) -> None:
            writer.write((line + "\r\n").encode())

        say("220 stub ready")
        while line := (await reader.readline()).decode().strip():
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                say("250 stub")
            elif verb == "MAIL":
                say("451 try later" if fail else "250 ok")
            elif verb in ("RCPT", "RSET", "NOOP"):
                say("250 ok")
            elif verb == "DATA":
                say("354 go ahead")
                await writer.drain()
                data = await reader.readuntil(b"\r\n.\r\n")
                received.append(message_from_bytes(data[:-5].replace(b"\r\n..", b"\r\n.")))
                say("250 queued")
            elif verb == "QUIT":
                say("221 bye")
                await writer.drain()
                break
            else:
                say("502 no")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, received
    finally:
        server.close()
        await server.wait_closed()


async def test_sender_delivers_pending_mail_and_marks_it_sent(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status="confirmed")
    await admin_advance(shop, n)
    async with smtp_stub() as (port, received):
        assert await sender.run_once(host="127.0.0.1", port=port) >= 1
        assert await sender.run_once(host="127.0.0.1", port=port) == 0  # nothing left to send
    mine = [m for m in received if shop.number(n) in m["Subject"]]
    assert len(mine) == 1
    msg = mine[0]
    assert msg["To"] == await email_of(owner_engine, shop.org_id, shop.x)
    assert "Test shop" in msg["From"]
    assert msg.is_multipart() and {p.get_content_type() for p in msg.walk()} >= {"text/plain", "text/html"}
    (row,) = await outbox_rows(owner_engine, shop.org_id)
    assert row.status == "sent" and row.sent_at is not None


async def test_a_failed_send_backs_off_and_stays_pending(shop: Shop, owner_engine: AsyncEngine) -> None:
    n = await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status="confirmed")
    with clock.freeze(bs.NOW):  # queued and attempted at the same frozen "now"
        await admin_advance(shop, n)
        async with smtp_stub(fail=True) as (port, _):
            await sender.run_once(host="127.0.0.1", port=port)
    (row,) = await outbox_rows(owner_engine, shop.org_id)
    assert row.status == "pending" and row.attempts == 1 and row.next_attempt_at > bs.NOW
