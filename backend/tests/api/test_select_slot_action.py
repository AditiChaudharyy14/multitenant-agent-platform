"""The slot buttons: a message carrying {action: {type: select_slot, service, start, practitioner?}}.

Like the Confirm button, the server runs it deterministically before any model call:
it calls the org's create_booking tool (or reschedule_booking when the button carries a
booking_number), which only PROPOSES. The proposal can't be confirmed in that same turn;
the customer's next message (or the Confirm button) does it.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock
from app.agents.providers.fake import FakeProvider
from tests.api.test_confirm_action import logged_in, parse_sse
from tests.booking_support import NOW, booking, local, scalar
from tests.factories import Shop, Studio, insert_studio_booking

pytestmark = pytest.mark.usefixtures("as_app_user")


def cards_of(body: str) -> list[dict]:
    return [d["card"] for k, d in parse_sse(body) if k == "card"]


async def test_select_slot_proposes_without_the_model_deciding(
    studio: Studio, owner_engine: AsyncEngine, fake: FakeProvider
) -> None:
    with clock.freeze(NOW):
        async with await logged_in(studio, studio.x) as c:
            cid = (await c.post("/api/conversations")).json()["id"]
            fake.script += ["Here is the summary. Shall I confirm?"]
            r = await c.post(f"/api/conversations/{cid}/messages", json={"action": {
                "type": "select_slot", "service": "Swedish Massage", "start": "2026-11-10T14:00", "practitioner": None}})  # fmt: skip
            assert r.status_code == 200, r.text
            [summary] = [x for x in cards_of(r.text) if x["type"] == "booking_summary"]
            assert summary["when"] == "Tue 10 Nov 2026, 14:00" and summary["practitioner"] == "Asha Gurung"
            last = fake.requests[-1]
            assert "Asha Gurung" in last.system  # the server told the model the outcome
            assert "create_booking" not in {t.name for t in last.tools}  # and didn't let it redo it
            # The stored user message reads like what the customer asked for.
            stored = await scalar(owner_engine, studio, "SELECT content FROM messages WHERE org_id = :o AND role = 'user' "
                                                        "ORDER BY created_at DESC LIMIT 1")  # fmt: skip
            assert "Swedish Massage" in stored and "Tue 10 Nov 2026, 14:00" in stored
            assert await scalar(owner_engine, studio, "SELECT count(*) FROM bookings WHERE org_id = :o") == 0

            # The next turn's Confirm button commits it.
            fake.script += ["Booked!"]
            r2 = await c.post(f"/api/conversations/{cid}/messages",
                              json={"action": {"type": "confirm_proposal", "proposal_id": summary["proposal_id"]}})  # fmt: skip
    [done] = [x for x in cards_of(r2.text) if x["type"] == "booking_confirmed"]
    assert done["booking_number"] == studio.number(1001)
    assert (await booking(owner_engine, studio, 1001)).status == "confirmed"


async def test_select_slot_with_a_booking_number_reschedules(
    studio: Studio, owner_engine: AsyncEngine, fake: FakeProvider
) -> None:
    mine = await insert_studio_booking(owner_engine, studio, studio.x, "swedish", "asha", local(2026, 11, 10, 10))
    with clock.freeze(NOW):
        async with await logged_in(studio, studio.x) as c:
            cid = (await c.post("/api/conversations")).json()["id"]
            fake.script += ["Here is the change."]
            r = await c.post(f"/api/conversations/{cid}/messages", json={"action": {
                "type": "select_slot", "service": "Swedish Massage", "start": "2026-11-12T16:00",
                "practitioner": "Asha Gurung", "booking_number": studio.number(mine)}})  # fmt: skip
    [summary] = [x for x in cards_of(r.text) if x["type"] == "booking_summary"]
    assert summary["kind"] == "reschedule_booking" and summary["old"]["when"] == "Tue 10 Nov 2026, 10:00"
    assert "reschedule_booking" not in {t.name for t in fake.requests[-1].tools}


async def test_select_slot_refusal_comes_from_the_server(
    studio: Studio, owner_engine: AsyncEngine, fake: FakeProvider
) -> None:
    with clock.freeze(NOW):
        async with await logged_in(studio, studio.x) as c:
            cid = (await c.post("/api/conversations")).json()["id"]
            fake.script += ["Sorry, that day is a holiday."]
            r = await c.post(f"/api/conversations/{cid}/messages", json={"action": {
                "type": "select_slot", "service": "Swedish Massage", "start": "2026-11-11T10:00"}})  # fmt: skip
    assert [x["type"] for x in cards_of(r.text)] == ["slot_alternatives"]
    assert "REFUSED" in fake.requests[-1].system


async def test_select_slot_is_rejected_for_a_store(shop: Shop, fake: FakeProvider) -> None:
    """The action runs a tool of the org's own vertical; a store has no create_booking."""
    async with await logged_in(shop, shop.x) as c:
        cid = (await c.post("/api/conversations")).json()["id"]
        r = await c.post(f"/api/conversations/{cid}/messages", json={"action": {
            "type": "select_slot", "service": "Swedish Massage", "start": "2026-11-10T14:00"}})  # fmt: skip
    assert r.status_code == 422
    assert fake.requests == []


async def test_select_slot_rejects_extra_fields(studio: Studio, fake: FakeProvider) -> None:
    async with await logged_in(studio, studio.x) as c:
        cid = (await c.post("/api/conversations")).json()["id"]
        r = await c.post(f"/api/conversations/{cid}/messages", json={"action": {
            "type": "select_slot", "service": "Swedish Massage", "start": "2026-11-10T14:00",
            "user_id": "00000000-0000-0000-0000-000000000000"}})  # fmt: skip
    assert r.status_code == 422
