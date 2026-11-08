"""Ten customers race for the same practitioner slot; the database lets exactly one win."""

import asyncio
import uuid
from datetime import timedelta

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import tenant_tx
from tests.factories import BASE_TIME, Tenant, add_customers, insert_booking

pytestmark = pytest.mark.usefixtures("as_app_user")

RACERS = 10


async def test_concurrent_bookings_for_same_slot_exactly_one_wins(
    tenants: tuple[Tenant, Tenant], owner_engine: AsyncEngine
) -> None:
    _, b = tenants
    # Distinct customers, so only the practitioner constraint can decide the race.
    customers = await add_customers(owner_engine, b, RACERS)
    slot = BASE_TIME + timedelta(days=20)
    # Every task holds its own open transaction here before inserting, so all ten
    # inserts are genuinely in flight at the same time.
    barrier = asyncio.Barrier(RACERS)

    async def book(user: uuid.UUID, wait: bool = True) -> tuple[int, BaseException | None]:
        pid = -1
        try:
            # One tenant_tx per task: its own pooled connection and its own transaction.
            async with tenant_tx(b.ctx(user, "customer")) as conn:
                pid = await conn.scalar(text("SELECT pg_backend_pid()"))
                if wait:
                    await barrier.wait()
                await insert_booking(conn, b, user, b.p1, slot)
            return pid, None
        except DBAPIError as exc:
            return pid, exc.orig.__cause__

    results = await asyncio.gather(*(book(c) for c in customers))

    pids = {pid for pid, _ in results}
    assert len(pids) == RACERS, "each racer must run on its own database connection"
    losers = [(c, err) for c, (_, err) in zip(customers, results, strict=True) if err is not None]
    assert len(losers) == RACERS - 1

    # Under real concurrency a loser fails in one of two ways. Each insert adds its GiST
    # entry and then waits on any in-flight conflicting transaction, so two losers can
    # wait on each other; Postgres then aborts one with 40P01 deadlock_detected instead
    # of 23P01 exclusion_violation. Nothing else is acceptable.
    for _, err in losers:
        assert isinstance(err, asyncpg.ExclusionViolationError | asyncpg.DeadlockDetectedError), repr(err)
        if isinstance(err, asyncpg.ExclusionViolationError):
            assert err.constraint_name == "no_practitioner_overlap"

    # A deadlocked loser, retried once (as the booking code must), sees the winner and
    # gets the ordinary exclusion_violation that triggers "offer 3 nearest slots".
    for user, err in losers:
        if isinstance(err, asyncpg.DeadlockDetectedError):
            _, retry_err = await book(user, wait=False)
            assert isinstance(retry_err, asyncpg.ExclusionViolationError), repr(retry_err)
            assert retry_err.constraint_name == "no_practitioner_overlap"

    async with tenant_tx(b.admin_ctx()) as conn:
        winners = await conn.scalar(
            text(
                "SELECT count(*) FROM bookings WHERE practitioner_id = :p "
                "AND starts_at = :s AND status = 'confirmed'"
            ),
            {"p": b.p1, "s": slot},
        )
    assert winners == 1
