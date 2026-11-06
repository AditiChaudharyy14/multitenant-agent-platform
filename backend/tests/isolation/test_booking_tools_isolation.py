"""Every booking tool, given someone else's booking number, says "not found" and changes nothing.

Customer X tries customer Y's booking (same org: RLS own_rows hides it) and a booking
from another org (tenant_isolation hides it). Every reply is identical to the reply for
a number that doesn't exist, so a guess can't tell "exists, not yours" from "absent".
Runs as app_user: RLS really is in force.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock
from app.agents.tools.booking import cancel_booking, list_my_bookings, reschedule_booking
from tests.booking_support import NOW, call, card, db_snapshot, iso, local, turn_ctx
from tests.factories import Studio, insert_studio_booking

pytestmark = pytest.mark.usefixtures("as_app_user")

CASES = {
    "reschedule_booking": (reschedule_booking, {"new_start": iso(local(2026, 11, 12, 16))}),
    "cancel_booking": (cancel_booking, {}),
}


@pytest.mark.parametrize("name", CASES)
async def test_foreign_booking_numbers_are_not_found(
    name: str, studio: Studio, other_studio: Studio, owner_engine: AsyncEngine
) -> None:
    tool, extra = CASES[name]
    tue = local(2026, 11, 10, 14)
    y_number = await insert_studio_booking(owner_engine, studio, studio.y, "swedish", "bina", tue)
    other_number = await insert_studio_booking(owner_engine, other_studio, other_studio.x, "swedish", "asha", tue)
    # X has a booking of their own, so "not found" can't be explained by having none.
    await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", tue)
    before = (await db_snapshot(owner_engine, studio), await db_snapshot(owner_engine, other_studio))

    ctx = turn_ctx(studio, studio.x)
    with clock.freeze(NOW):
        replies = [
            await call(tool, ctx, booking_number=studio.number(y_number), **extra),  # same org, other customer
            await call(tool, ctx, booking_number=other_studio.number(other_number), **extra),  # other org's prefix
            await call(tool, ctx, booking_number=studio.number(other_number + 100), **extra),  # nonexistent
            await call(tool, ctx, booking_number="9999", **extra),  # bare number, nonexistent
        ]
    texts = {r.content_for_model for r in replies}
    assert len(texts) == 1, texts
    assert next(iter(texts)).startswith("NOT FOUND")
    assert all(r.cards == [] for r in replies)
    assert (await db_snapshot(owner_engine, studio), await db_snapshot(owner_engine, other_studio)) == before


async def test_list_my_bookings_shows_only_my_own(studio: Studio, other_studio: Studio, owner_engine: AsyncEngine) -> None:
    tue = local(2026, 11, 10, 14)
    mine = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", tue)
    await insert_studio_booking(owner_engine, studio, studio.y, "swedish", "bina", tue)
    await insert_studio_booking(owner_engine, other_studio, other_studio.x, "swedish", "asha", tue)
    with clock.freeze(NOW):
        r = await call(list_my_bookings, turn_ctx(studio, studio.x))
    c = card(r, "booking_list")
    assert [b["booking_number"] for b in c["upcoming"] + c["past"]] == [studio.number(mine)]


async def test_busy_time_of_others_is_visible_only_as_busy(studio: Studio, owner_engine: AsyncEngine) -> None:
    """Y's booking blocks the slot for X (via busy_intervals), but nothing about Y leaks."""
    from app.agents.tools.booking import create_booking

    tue = local(2026, 11, 10, 14)
    await insert_studio_booking(owner_engine, studio, studio.y, "swedish", "asha", tue)
    with clock.freeze(NOW):
        r = await call(create_booking, turn_ctx(studio, studio.x), service="swedish", start=iso(tue), practitioner="Asha")
    assert r.content_for_model.startswith("REFUSED")
    assert studio.number(1001) not in r.content_for_model  # Y's booking number is never revealed
