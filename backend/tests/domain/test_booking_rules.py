"""Booking business rules, through the tool handlers exactly as the agent loop calls them.

All under a frozen clock: NOW is Thu 5 Nov 2026, 14:00 NPT. The studio (tests/factories.py)
opens Mon–Thu 09–19, Fri 09–15, Sun 09–19, is closed on Saturday and on the holiday Wed
11 Nov. Asha and Bina both offer Swedish (60 min, NPR 3,500) and Deep Tissue (90 min);
only Bina offers Private Yoga.

Mutating tools only propose; confirm_proposal, in a later turn, commits.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock
from app.agents.tools.booking import (
    cancel_booking,
    create_booking,
    find_available_slots,
    get_opening_hours,
    list_my_bookings,
    list_services,
    reschedule_booking,
)
from app.agents.tools.common.confirm_proposal import confirm_proposal
from tests.booking_support import (
    NOW,
    TZ,
    booking,
    call,
    card,
    db_snapshot,
    iso,
    local,
    proposal_id,
    rows,
    scalar,
    set_service_price,
    slot_starts,
    summary,
    turn_ctx,
)
from tests.factories import Studio, insert_studio_booking, seed_studio

pytestmark = pytest.mark.usefixtures("as_app_user")

TUE_14 = local(2026, 11, 10, 14, 0)


async def booking_count(owner: AsyncEngine, studio: Studio) -> int:
    return await scalar(owner, studio, "SELECT count(*) FROM bookings WHERE org_id = :o")


async def propose_and_confirm(studio: Studio, user, tool, **args):  # noqa: ANN001, ANN201
    """Propose in one turn, confirm in the next. Returns (proposal result, confirm result)."""
    proposed = await call(tool, turn_ctx(studio, user), **args)
    confirmed = await call(confirm_proposal, turn_ctx(studio, user), proposal_id=proposal_id(proposed))
    return proposed, confirmed


# ───────────── catalogue and hours ─────────────


async def test_list_services_has_live_prices_durations_and_practitioners(studio: Studio) -> None:
    with clock.freeze(NOW):
        r = await call(list_services, turn_ctx(studio, studio.x))
    services = {s["name"]: s for s in card(r, "service_list")["services"]}
    assert services["Swedish Massage"]["duration_min"] == 60
    assert services["Swedish Massage"]["price"] == "3500.00"
    assert services["Swedish Massage"]["price_display"] == "NPR 3,500.00"
    assert services["Private Yoga"]["practitioners"] == ["Bina Shrestha"]
    assert sorted(services["Deep Tissue Massage"]["practitioners"]) == ["Asha Gurung", "Bina Shrestha"]
    assert "NPR 3,500.00" in r.content_for_model


async def test_list_services_reads_the_database_not_a_cache(studio: Studio, owner_engine: AsyncEngine) -> None:
    await set_service_price(owner_engine, studio, "swedish", "3900.00")
    with clock.freeze(NOW):
        r = await call(list_services, turn_ctx(studio, studio.x))
    assert "NPR 3,900.00" in r.content_for_model


async def test_opening_hours_show_closed_days_and_upcoming_holidays(studio: Studio) -> None:
    with clock.freeze(NOW):
        r = await call(get_opening_hours, turn_ctx(studio, studio.x))
    c = card(r, "opening_hours")
    week = {d["day"]: d for d in c["week"]}
    assert week["Saturday"]["closed"] is True
    assert (week["Friday"]["opens"], week["Friday"]["closes"]) == ("09:00", "15:00")
    assert [h["date"] for h in c["holidays"]] == ["Wed 11 Nov 2026"]
    assert "Saturday: closed" in r.content_for_model


# ───────────── finding slots ─────────────


async def test_find_slots_next_tuesday_afternoon(studio: Studio) -> None:
    with clock.freeze(NOW):
        r = await call(find_available_slots, turn_ctx(studio, studio.x), service="swedish", when="next Tuesday afternoon")
    c = card(r, "slot_list")
    assert c["resolved_dates"] == ["Tue 10 Nov 2026"]
    starts = slot_starts(r)
    assert starts and all(s.startswith("2026-11-10T") for s in starts)
    assert all("12:00" <= s[11:] < "17:00" for s in starts)
    first = c["slots"][0]
    # Each slot is a button that books exactly that slot.
    assert first["action"] == {"type": "select_slot", "service": "Swedish Massage", "practitioner": None, "start": first["start"]}
    assert first["display"] == "Tue 10 Nov 2026, 12:00"
    assert "Tue 10 Nov 2026" in r.content_for_model


async def test_find_slots_this_weekend_skips_closed_saturday(studio: Studio) -> None:
    with clock.freeze(NOW):
        r = await call(find_available_slots, turn_ctx(studio, studio.x), service="Swedish Massage", when="this weekend")
    starts = slot_starts(r)
    assert starts and all(s.startswith("2026-11-08") for s in starts)  # Sunday only
    assert "Sat 7 Nov 2026" in r.content_for_model and "closed" in r.content_for_model


async def test_find_slots_ambiguous_next_friday_asks(studio: Studio) -> None:
    with clock.freeze(NOW):
        r = await call(find_available_slots, turn_ctx(studio, studio.x), service="swedish", when="next Friday")
    assert r.content_for_model.startswith("AMBIGUOUS")
    assert "Fri 6 Nov 2026" in r.content_for_model and "Fri 13 Nov 2026" in r.content_for_model
    assert not [c for c in r.cards if c["type"] == "slot_list"]


async def test_find_slots_on_a_holiday_says_so(studio: Studio) -> None:
    with clock.freeze(NOW):
        r = await call(find_available_slots, turn_ctx(studio, studio.x), service="swedish", when="2026-11-11")
    assert slot_starts(r) == []
    assert "Tihar" in r.content_for_model


async def test_find_slots_for_a_practitioner_hides_their_busy_time(studio: Studio, owner_engine: AsyncEngine) -> None:
    await insert_studio_booking(owner_engine, studio, studio.y, "swedish", "asha", TUE_14)
    with clock.freeze(NOW):
        r = await call(find_available_slots, turn_ctx(studio, studio.x), service="swedish",
                       when="next Tuesday afternoon", practitioner="Asha")  # fmt: skip
    starts = slot_starts(r)
    assert "2026-11-10T13:00" in starts and "2026-11-10T15:00" in starts
    assert not {"2026-11-10T13:15", "2026-11-10T14:00", "2026-11-10T14:45"} & set(starts)
    assert all(s["practitioner"] == "Asha Gurung" for s in card(r, "slot_list")["slots"])


async def test_find_slots_needs_a_known_service(studio: Studio) -> None:
    with clock.freeze(NOW):
        r = await call(find_available_slots, turn_ctx(studio, studio.x), service="reiki", when="tomorrow")
    assert not r.ok and "Swedish Massage" in r.content_for_model  # lists the real choices


async def test_find_slots_past_date_is_refused(studio: Studio) -> None:
    with clock.freeze(NOW):
        r = await call(find_available_slots, turn_ctx(studio, studio.x), service="swedish", when="1 Nov 2026")
    assert "past" in r.content_for_model and not [c for c in r.cards if c["type"] == "slot_list"]


# ───────────── invalid slots: refused, no proposal ─────────────


@pytest.mark.parametrize(
    ("service", "start", "reason"),
    [
        ("swedish", local(2026, 11, 11, 10), "holiday"),
        ("swedish", local(2026, 11, 7, 10), "closed"),  # Saturday
        ("swedish", local(2026, 11, 10, 7), "opening hours"),
        ("swedish", local(2026, 11, 10, 18, 30), "closing"),  # 60 min past 19:00
        ("deep", local(2026, 11, 6, 14), "closing"),  # 90 min on a Friday that closes at 15:00
        ("swedish", local(2026, 11, 5, 14, 30), "60 minutes"),  # too soon
        ("swedish", local(2026, 11, 10, 10, 10), "15-minute"),
        ("swedish", local(2026, 11, 2, 10), "60 minutes"),  # in the past
    ],
    ids=["holiday", "saturday", "before-opening", "overflow", "overflow-friday", "too-soon", "off-step", "past"],
)
async def test_invalid_slots_are_refused_without_a_proposal(
    studio: Studio, owner_engine: AsyncEngine, service: str, start, reason: str  # noqa: ANN001
) -> None:
    before = await db_snapshot(owner_engine, studio)
    with clock.freeze(NOW):
        r = await call(create_booking, turn_ctx(studio, studio.x), service=service, start=iso(start))
    assert r.content_for_model.startswith("REFUSED"), r.content_for_model
    assert reason in r.content_for_model
    assert not [c for c in r.cards if c["type"] == "booking_summary"]
    assert len(slot_starts(r, "slot_alternatives")) == 3
    assert await db_snapshot(owner_engine, studio) == before


async def test_service_a_practitioner_does_not_offer_is_refused(studio: Studio) -> None:
    with clock.freeze(NOW):
        r = await call(create_booking, turn_ctx(studio, studio.x), service="Private Yoga", start=iso(TUE_14), practitioner="Asha")
    assert r.content_for_model.startswith("REFUSED") and "Bina Shrestha" in r.content_for_model


async def test_practitioner_double_booking_is_refused_with_three_nearest(studio: Studio, owner_engine: AsyncEngine) -> None:
    await insert_studio_booking(owner_engine, studio, studio.y, "swedish", "asha", TUE_14)
    before = await db_snapshot(owner_engine, studio)
    with clock.freeze(NOW):
        r = await call(create_booking, turn_ctx(studio, studio.x), service="swedish", start=iso(TUE_14), practitioner="Asha Gurung")
    assert r.content_for_model.startswith("REFUSED") and "Asha Gurung" in r.content_for_model
    alts = card(r, "slot_alternatives")["slots"]
    # Asha's own nearest free starts by time difference; a tie (13:00 vs 15:00) goes to the earlier.
    assert [(a["start"], a["practitioner"]) for a in alts] == [
        ("2026-11-10T13:00", "Asha Gurung"), ("2026-11-10T15:00", "Asha Gurung"), ("2026-11-10T12:45", "Asha Gurung"),
    ]  # fmt: skip
    assert all(a["action"]["type"] == "select_slot" for a in alts)
    assert await db_snapshot(owner_engine, studio) == before


async def test_customer_cannot_overlap_their_own_booking_with_another_practitioner(
    studio: Studio, owner_engine: AsyncEngine
) -> None:
    await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", TUE_14)
    with clock.freeze(NOW):
        r = await call(create_booking, turn_ctx(studio, studio.x), service="swedish",
                       start=iso(TUE_14 + timedelta(minutes=30)), practitioner="Bina")  # fmt: skip
    assert r.content_for_model.startswith("REFUSED")
    assert "already have a booking" in r.content_for_model
    # Alternatives must not overlap the customer's own booking either.
    assert "2026-11-10T14:15" not in slot_starts(r, "slot_alternatives")


# ───────────── proposing and confirming ─────────────


async def test_booking_summary_card_has_everything_the_customer_must_see(studio: Studio, owner_engine: AsyncEngine) -> None:
    with clock.freeze(NOW):
        r = await call(create_booking, turn_ctx(studio, studio.x), service="swedish massage", start=iso(TUE_14), practitioner="bina")
    s = summary(r)
    assert s["kind"] == "create_booking"
    assert s["service"] == "Swedish Massage"
    assert s["practitioner"] == "Bina Shrestha"
    assert s["date"] == "Tue 10 Nov 2026" and s["time"] == "14:00"
    assert s["when"] == "Tue 10 Nov 2026, 14:00"
    assert s["duration_min"] == 60
    assert s["price"] == "3500.00" and s["price_display"] == "NPR 3,500.00"
    assert s["actions"] == [{"type": "confirm_proposal", "proposal_id": s["proposal_id"], "label": "Confirm booking"}]
    assert r.content_for_model.startswith("PROPOSED, NOT DONE YET")
    assert await booking_count(owner_engine, studio) == 0  # proposing writes no booking


async def test_no_practitioner_given_picks_a_free_one_and_says_who(studio: Studio, owner_engine: AsyncEngine) -> None:
    await insert_studio_booking(owner_engine, studio, studio.y, "swedish", "asha", TUE_14)
    with clock.freeze(NOW):
        r = await call(create_booking, turn_ctx(studio, studio.x), service="swedish", start=iso(TUE_14))
    assert summary(r)["practitioner"] == "Bina Shrestha"
    assert "Bina Shrestha" in r.content_for_model


async def test_confirm_in_a_later_turn_creates_the_booking(studio: Studio, owner_engine: AsyncEngine) -> None:
    with clock.freeze(NOW):
        proposed, confirmed = await propose_and_confirm(studio, studio.x, create_booking, service="swedish", start=iso(TUE_14))
    assert confirmed.content_for_model.startswith("CONFIRMED"), confirmed.content_for_model
    done = card(confirmed, "booking_confirmed")
    assert done["booking_number"] == studio.number(1001)
    assert done["when"] == "Tue 10 Nov 2026, 14:00" and done["practitioner"] == "Asha Gurung"
    b = await booking(owner_engine, studio, 1001)
    assert b.status == "confirmed" and b.user_id == studio.x
    assert b.starts_at == TUE_14 and b.ends_at == TUE_14 + timedelta(minutes=60)
    assert b.price == Decimal("3500.00")
    assert str(b.proposal_id) == proposal_id(proposed)


async def test_confirm_in_the_same_turn_is_refused(studio: Studio, owner_engine: AsyncEngine) -> None:
    ctx = turn_ctx(studio, studio.x)
    with clock.freeze(NOW):
        proposed = await call(create_booking, ctx, service="swedish", start=iso(TUE_14))
        r = await call(confirm_proposal, ctx, proposal_id=proposal_id(proposed))
    assert r.content_for_model.startswith("REFUSED")
    assert await booking_count(owner_engine, studio) == 0


async def test_double_confirm_books_once(studio: Studio, owner_engine: AsyncEngine) -> None:
    with clock.freeze(NOW):
        proposed, _ = await propose_and_confirm(studio, studio.x, create_booking, service="swedish", start=iso(TUE_14))
        again = await call(confirm_proposal, turn_ctx(studio, studio.x), proposal_id=proposal_id(proposed))
    assert "already confirmed" in again.content_for_model
    assert await booking_count(owner_engine, studio) == 1


async def test_already_confirmed_booking_refusal_names_the_booking_and_its_status(studio: Studio, owner_engine: AsyncEngine) -> None:
    """The refusal carries the booking number and its live status, so the model never guesses."""
    with clock.freeze(NOW):
        proposed, _ = await propose_and_confirm(studio, studio.x, create_booking, service="swedish", start=iso(TUE_14))
        again = await call(confirm_proposal, turn_ctx(studio, studio.x), proposal_id=proposal_id(proposed))
    assert studio.number(1001) in again.content_for_model and "status: confirmed" in again.content_for_model

    # Cancelled later (in another conversation, say): a re-press now reports that, live.
    with clock.freeze(NOW):
        cancel, _ = await propose_and_confirm(studio, studio.x, cancel_booking, booking_number=studio.number(1001))
        late = await call(confirm_proposal, turn_ctx(studio, studio.x), proposal_id=proposal_id(proposed))
        cancel_again = await call(confirm_proposal, turn_ctx(studio, studio.x), proposal_id=proposal_id(cancel))
    assert studio.number(1001) in late.content_for_model and "status: cancelled" in late.content_for_model
    assert studio.number(1001) in cancel_again.content_for_model and "status: cancelled" in cancel_again.content_for_model


async def test_price_change_before_confirm_issues_a_fresh_proposal(studio: Studio, owner_engine: AsyncEngine) -> None:
    with clock.freeze(NOW):
        proposed = await call(create_booking, turn_ctx(studio, studio.x), service="swedish", start=iso(TUE_14))
        await set_service_price(owner_engine, studio, "swedish", "3900.00")
        r = await call(confirm_proposal, turn_ctx(studio, studio.x), proposal_id=proposal_id(proposed))
    assert r.content_for_model.startswith("CHANGED")
    assert "NPR 3,500.00" in r.content_for_model and "NPR 3,900.00" in r.content_for_model
    fresh = summary(r)
    assert fresh["price"] == "3900.00" and fresh["proposal_id"] != proposal_id(proposed)
    assert await booking_count(owner_engine, studio) == 0
    statuses = dict(await rows(owner_engine, studio, "SELECT id::text, status FROM proposals WHERE org_id = :o"))
    assert statuses[proposal_id(proposed)] == "superseded" and statuses[fresh["proposal_id"]] == "open"


async def test_slot_taken_before_confirm_is_refused_with_alternatives(studio: Studio, owner_engine: AsyncEngine) -> None:
    with clock.freeze(NOW):
        proposed = await call(create_booking, turn_ctx(studio, studio.x), service="swedish", start=iso(TUE_14), practitioner="Asha")
        await insert_studio_booking(owner_engine, studio, studio.y, "swedish", "asha", TUE_14)
        r = await call(confirm_proposal, turn_ctx(studio, studio.x), proposal_id=proposal_id(proposed))
    assert r.content_for_model.startswith("REFUSED")
    assert len(slot_starts(r, "slot_alternatives")) == 3
    assert await booking_count(owner_engine, studio) == 1  # only Y's


async def test_auto_picked_practitioner_taken_before_confirm_offers_another(studio: Studio, owner_engine: AsyncEngine) -> None:
    with clock.freeze(NOW):
        proposed = await call(create_booking, turn_ctx(studio, studio.x), service="swedish", start=iso(TUE_14))
        assert summary(proposed)["practitioner"] == "Asha Gurung"
        await insert_studio_booking(owner_engine, studio, studio.y, "swedish", "asha", TUE_14)
        r = await call(confirm_proposal, turn_ctx(studio, studio.x), proposal_id=proposal_id(proposed))
    assert r.content_for_model.startswith("CHANGED")
    assert summary(r)["practitioner"] == "Bina Shrestha" and summary(r)["when"] == "Tue 10 Nov 2026, 14:00"
    assert await booking_count(owner_engine, studio) == 1


async def test_booking_numbers_are_sequential_per_org(studio: Studio, owner_engine: AsyncEngine) -> None:
    other = await seed_studio(owner_engine)
    with clock.freeze(NOW):
        _, a = await propose_and_confirm(studio, studio.x, create_booking, service="swedish", start=iso(TUE_14))
        _, b = await propose_and_confirm(studio, studio.y, create_booking, service="swedish", start=iso(TUE_14))
        _, c = await propose_and_confirm(other, other.x, create_booking, service="swedish", start=iso(TUE_14))
    assert card(a, "booking_confirmed")["booking_number"] == studio.number(1001)
    assert card(b, "booking_confirmed")["booking_number"] == studio.number(1002)
    assert card(c, "booking_confirmed")["booking_number"] == other.number(1001)


# ───────────── reschedule ─────────────


async def test_reschedule_into_a_conflict_is_refused_and_nothing_changes(studio: Studio, owner_engine: AsyncEngine) -> None:
    mine = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", local(2026, 11, 10, 10))
    await insert_studio_booking(owner_engine, studio, studio.y, "swedish", "asha", TUE_14)
    before = await db_snapshot(owner_engine, studio)
    with clock.freeze(NOW):
        r = await call(reschedule_booking, turn_ctx(studio, studio.x), booking_number=studio.number(mine), new_start=iso(TUE_14))
    assert r.content_for_model.startswith("REFUSED")
    alts = card(r, "slot_alternatives")["slots"]
    assert len(alts) == 3 and all(a["practitioner"] == "Asha Gurung" for a in alts)
    # The alternatives' buttons reschedule this booking rather than make a new one.
    assert all(a["action"]["booking_number"] == studio.number(mine) for a in alts)
    assert not [c for c in r.cards if c["type"] == "booking_summary"]
    assert await db_snapshot(owner_engine, studio) == before


async def test_reschedule_into_a_free_slot_moves_the_booking(studio: Studio, owner_engine: AsyncEngine) -> None:
    mine = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", local(2026, 11, 10, 10))
    new = local(2026, 11, 12, 16)
    with clock.freeze(NOW):
        proposed = await call(reschedule_booking, turn_ctx(studio, studio.x), booking_number=studio.number(mine), new_start=iso(new))
        s = summary(proposed)
        assert s["kind"] == "reschedule_booking" and s["booking_number"] == studio.number(mine)
        assert s["old"]["when"] == "Tue 10 Nov 2026, 10:00" and s["when"] == "Thu 12 Nov 2026, 16:00"
        assert s["practitioner"] == "Asha Gurung" and s["service"] == "Swedish Massage"
        r = await call(confirm_proposal, turn_ctx(studio, studio.x), proposal_id=s["proposal_id"])
    assert r.content_for_model.startswith("CONFIRMED"), r.content_for_model
    b = await booking(owner_engine, studio, mine)
    assert b.starts_at == new and b.ends_at == new + timedelta(minutes=60) and b.status == "confirmed"
    assert card(r, "booking_rescheduled")["when"] == "Thu 12 Nov 2026, 16:00"


async def test_reschedule_may_overlap_its_own_old_time(studio: Studio, owner_engine: AsyncEngine) -> None:
    mine = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", local(2026, 11, 10, 10))
    with clock.freeze(NOW):
        _, r = await propose_and_confirm(studio, studio.x, reschedule_booking,
                                         booking_number=studio.number(mine), new_start=iso(local(2026, 11, 10, 10, 30)))  # fmt: skip
    assert r.content_for_model.startswith("CONFIRMED"), r.content_for_model


async def test_reschedule_to_another_practitioner(studio: Studio, owner_engine: AsyncEngine) -> None:
    mine = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", local(2026, 11, 10, 10))
    with clock.freeze(NOW):
        _, r = await propose_and_confirm(studio, studio.x, reschedule_booking, booking_number=studio.number(mine),
                                         new_start=iso(TUE_14), practitioner="Bina")  # fmt: skip
    assert r.content_for_model.startswith("CONFIRMED")
    assert (await booking(owner_engine, studio, mine)).practitioner_name == "Bina Shrestha"


async def test_slot_taken_before_reschedule_confirm_leaves_booking_alone(studio: Studio, owner_engine: AsyncEngine) -> None:
    mine = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", local(2026, 11, 10, 10))
    with clock.freeze(NOW):
        proposed = await call(reschedule_booking, turn_ctx(studio, studio.x), booking_number=studio.number(mine), new_start=iso(TUE_14))
        await insert_studio_booking(owner_engine, studio, studio.y, "swedish", "asha", TUE_14)
        r = await call(confirm_proposal, turn_ctx(studio, studio.x), proposal_id=proposal_id(proposed))
    assert r.content_for_model.startswith("REFUSED") and len(slot_starts(r, "slot_alternatives")) == 3
    assert (await booking(owner_engine, studio, mine)).starts_at == local(2026, 11, 10, 10)


# ───────────── cancel ─────────────


async def test_cancel_more_than_24h_ahead_is_free(studio: Studio, owner_engine: AsyncEngine) -> None:
    mine = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", TUE_14)
    with clock.freeze(NOW):
        proposed, r = await propose_and_confirm(studio, studio.x, cancel_booking, booking_number=studio.number(mine))
    s = summary(proposed)
    assert s["kind"] == "cancel_booking" and s["fee"] == "0.00" and s["fee_display"] == "NPR 0.00"
    assert r.content_for_model.startswith("CONFIRMED")
    b = await booking(owner_engine, studio, mine)
    assert b.status == "cancelled" and b.cancellation_fee == Decimal("0.00")


async def test_cancel_within_24h_costs_half_the_price(studio: Studio, owner_engine: AsyncEngine) -> None:
    mine = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", local(2026, 11, 6, 10))  # in 20 h
    with clock.freeze(NOW):
        proposed, r = await propose_and_confirm(studio, studio.x, cancel_booking, booking_number=studio.number(mine))
    s = summary(proposed)
    assert s["fee"] == "1750.00" and s["fee_display"] == "NPR 1,750.00"
    assert "50%" in proposed.content_for_model
    assert r.content_for_model.startswith("CONFIRMED")
    b = await booking(owner_engine, studio, mine)
    assert b.status == "cancelled" and b.cancellation_fee == Decimal("1750.00")


async def test_crossing_the_24h_line_before_confirm_requotes_the_fee(studio: Studio, owner_engine: AsyncEngine) -> None:
    mine = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", local(2026, 11, 6, 14))
    with clock.freeze(local(2026, 11, 5, 13, 55)):  # 24h05m ahead: free
        proposed = await call(cancel_booking, turn_ctx(studio, studio.x), booking_number=studio.number(mine))
    assert summary(proposed)["fee"] == "0.00"
    with clock.freeze(local(2026, 11, 5, 14, 5)):  # 23h55m ahead: the fee applies now
        r = await call(confirm_proposal, turn_ctx(studio, studio.x), proposal_id=proposal_id(proposed))
    assert r.content_for_model.startswith("CHANGED")
    assert summary(r)["fee"] == "1750.00"
    assert (await booking(owner_engine, studio, mine)).status == "confirmed"


# ───────────── past, cancelled and completed bookings ─────────────


@pytest.mark.parametrize(
    ("start", "status"),
    [
        (local(2026, 11, 2, 10), "completed"),
        (local(2026, 11, 5, 13, 30), "confirmed"),  # already started
        (local(2026, 11, 3, 10), "confirmed"),  # in the past, never marked completed
        (TUE_14, "cancelled"),
    ],
    ids=["completed", "started", "past-confirmed", "cancelled"],
)
@pytest.mark.parametrize("tool", [reschedule_booking, cancel_booking], ids=lambda t: t.name)
async def test_only_confirmed_future_bookings_can_change(
    studio: Studio, owner_engine: AsyncEngine, start, status: str, tool  # noqa: ANN001
) -> None:
    mine = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", start, status=status)
    extra = {"new_start": iso(local(2026, 11, 12, 16))} if tool is reschedule_booking else {}
    before = await db_snapshot(owner_engine, studio)
    with clock.freeze(NOW):
        r = await call(tool, turn_ctx(studio, studio.x), booking_number=studio.number(mine), **extra)
    assert r.content_for_model.startswith("REFUSED"), r.content_for_model
    assert studio.number(mine) in r.content_for_model
    assert not [c for c in r.cards if c["type"] == "booking_summary"]
    assert await db_snapshot(owner_engine, studio) == before


# ───────────── list ─────────────


async def test_list_my_bookings_splits_upcoming_and_past(studio: Studio, owner_engine: AsyncEngine) -> None:
    past = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", local(2026, 11, 2, 10), status="completed")
    up = await insert_studio_booking(owner_engine, studio, studio.x, "deep", "bina", TUE_14)
    gone = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", local(2026, 11, 12, 10), status="cancelled")
    await insert_studio_booking(owner_engine, studio, studio.y, "swedish", "asha", TUE_14)  # someone else's
    with clock.freeze(NOW):
        r = await call(list_my_bookings, turn_ctx(studio, studio.x))
    c = card(r, "booking_list")
    assert [b["booking_number"] for b in c["upcoming"]] == [studio.number(up)]
    assert {b["booking_number"] for b in c["past"]} == {studio.number(past), studio.number(gone)}
    assert c["upcoming"][0]["when"] == "Tue 10 Nov 2026, 14:00"
    assert c["upcoming"][0]["service"] == "Deep Tissue Massage"
    assert len(c["upcoming"]) + len(c["past"]) == 3


async def test_start_times_are_stored_in_utc(studio: Studio, owner_engine: AsyncEngine) -> None:
    with clock.freeze(NOW):
        await propose_and_confirm(studio, studio.x, create_booking, service="swedish", start="2026-11-10T09:00")
    b = await booking(owner_engine, studio, 1001)
    assert b.starts_at.astimezone(TZ).hour == 9
    assert (b.starts_at.utcoffset(), b.starts_at.hour, b.starts_at.minute) == (timedelta(0), 3, 15)
