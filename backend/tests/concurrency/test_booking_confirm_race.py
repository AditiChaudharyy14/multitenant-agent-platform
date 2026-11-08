"""Ten customers confirm a booking for the same practitioner slot at the same moment,
through the real tool path (create_booking, then confirm_proposal in a later turn).

Exactly one booking is made. Each of the nine losers gets a refusal with the three
nearest free alternatives; no database error escapes to the agent. Under real
concurrency a loser's insert may die with deadlock_detected instead of
exclusion_violation (see test_booking_race.py); the committer retries that once and
then treats it as a conflict.
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock
from app.agents.tools.booking import create_booking
from app.agents.tools.common.confirm_proposal import confirm_proposal
from tests.booking_support import NOW, call, iso, local, proposal_id, scalar, slot_starts, turn_ctx
from tests.factories import Studio, add_studio_customers

pytestmark = pytest.mark.usefixtures("as_app_user")

RACERS = 10
SLOT = local(2026, 11, 10, 14, 0)


async def test_ten_concurrent_confirms_make_exactly_one_booking(studio: Studio, owner_engine: AsyncEngine) -> None:
    customers = await add_studio_customers(owner_engine, studio, RACERS)
    with clock.freeze(NOW):
        # Turn 1 for everyone: each gets a valid proposal, since nothing is booked yet.
        proposals = [
            proposal_id(
                await call(create_booking, turn_ctx(studio, c), service="swedish", start=iso(SLOT), practitioner="Asha")
            )
            for c in customers
        ]
        # Turn 2, all at once. gather() starts every confirm before any finishes.
        results = await asyncio.gather(*(
            call(confirm_proposal, turn_ctx(studio, c), proposal_id=p) for c, p in zip(customers, proposals, strict=True)
        ))  # fmt: skip

    outcomes = [r.content_for_model.split(":", 1)[0] for r in results]
    assert outcomes.count("CONFIRMED") == 1, outcomes
    assert outcomes.count("REFUSED") == RACERS - 1, outcomes
    for r in results:
        if r.content_for_model.startswith("REFUSED"):
            assert len(slot_starts(r, "slot_alternatives")) == 3
            assert iso(SLOT) not in slot_starts(r, "slot_alternatives")

    booked = await scalar(
        owner_engine, studio,
        "SELECT count(*) FROM bookings WHERE org_id = :o AND starts_at = :s AND status = 'confirmed'",
        s=SLOT,
    )  # fmt: skip
    assert booked == 1
    statuses = await scalar(
        owner_engine, studio,
        "SELECT array_agg(status ORDER BY status) FROM proposals WHERE org_id = :o",
    )  # fmt: skip
    assert sorted(statuses) == ["confirmed"] + ["rejected"] * (RACERS - 1)
