"""The 24-hour booking reminder scan. Idempotent twice over: bookings.reminder_sent_at is set
in the same transaction as the outbox row, and the row's dedupe key includes the start.

Runs per org in an admin-scoped transaction as app_user (like `make advance`), so RLS
still applies and no owner credentials are needed. FOR UPDATE SKIP LOCKED lets two
scans (or a scan and a reschedule) never queue the same reminder twice.
"""

from datetime import timedelta

from sqlalchemy import text

from app import clock
from app.db.session import TenantContext, app_engine, tenant_tx
from app.domain.bookings.service import booking_number
from app.domain.notifications import triggers
from app.tenancy.orgs import get_org_by_id

WINDOW = timedelta(hours=24)


async def run_once() -> int:
    """Queue every due reminder in every org. Returns how many were queued."""
    async with app_engine.connect() as conn:  # orgs is global: app_user may read it
        org_ids = list(await conn.scalars(text("SELECT id FROM orgs ORDER BY slug")))
    queued = 0
    now = clock.now()
    for org_id in org_ids:
        async with tenant_tx(TenantContext(org_id=org_id, user_id=None, role="admin")) as conn:
            org = await get_org_by_id(conn, org_id)
            if org is None or org.booking_prefix is None:
                continue  # an org without bookings (its vertical has none)
            due = (await conn.execute(text(
                "SELECT b.id, b.user_id, b.booking_number, b.starts_at, s.name AS service, p.name AS practitioner "
                "FROM bookings b JOIN services s ON s.id = b.service_id JOIN practitioners p ON p.id = b.practitioner_id "
                "WHERE b.status = 'confirmed' AND b.reminder_sent_at IS NULL AND b.starts_at > :now "
                "AND b.starts_at <= :until ORDER BY b.starts_at FOR UPDATE OF b SKIP LOCKED"),
                {"now": now, "until": now + WINDOW})).all()  # fmt: skip
            for b in due:
                if await triggers.booking_reminder(conn, org, booking_id=b.id, user_id=b.user_id,
                                                   number=booking_number(org, b.booking_number), service=b.service,
                                                   practitioner=b.practitioner, starts_at=b.starts_at):  # fmt: skip
                    queued += 1
                await conn.execute(
                    text("UPDATE bookings SET reminder_sent_at = :n WHERE id = :i"), {"n": now, "i": b.id}
                )
    return queued
