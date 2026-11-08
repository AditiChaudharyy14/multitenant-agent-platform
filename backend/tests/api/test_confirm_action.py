"""The Confirm button: a message carrying {action: {type: confirm_proposal, proposal_id}}.

The server commits the proposal deterministically BEFORE any model call; the model only
acknowledges the outcome, which reaches it through the server-written system prompt.
"""

import json
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.providers.base import ProviderUnavailable
from app.agents.providers.fake import FakeProvider, tool_call
from app.auth.sessions import COOKIE_NAME, create_session
from app.db.session import app_engine
from app.main import app
from tests.factories import Shop
from tests.store_support import scalar, stock

pytestmark = pytest.mark.usefixtures("as_app_user")

ORIGIN = "http://localhost:3000"


async def logged_in(shop: Shop, user: uuid.UUID) -> httpx.AsyncClient:
    async with app_engine.begin() as conn:
        token = await create_session(conn, shop.org_id, user, "customer")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                             headers={"Origin": ORIGIN}, cookies={COOKIE_NAME: token}, timeout=30)  # fmt: skip


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln and not ln.startswith(":")]
        if lines:
            kind = next(ln[7:] for ln in lines if ln.startswith("event: "))
            events.append((kind, json.loads("\n".join(ln[6:] for ln in lines if ln.startswith("data: ")))))
    return events


async def propose_via_chat(c: httpx.AsyncClient, fake: FakeProvider) -> tuple[str, str]:
    cid = (await c.post("/api/conversations")).json()["id"]
    fake.script += [
        tool_call("create_order", {"items": [{"sku": "GRN-1", "qty": 2}], "delivery_address": "House 7, Baluwatar"}),
        "Here is your order summary. Shall I place it?",
    ]
    r = await c.post(f"/api/conversations/{cid}/messages", json={"content": "Two Green Alpha please"})
    cards = [d["card"] for k, d in parse_sse(r.text) if k == "card"]
    [summary] = [x for x in cards if x["type"] == "order_summary"]
    return cid, summary["proposal_id"]


async def order_count(owner: AsyncEngine, shop: Shop) -> int:
    return await scalar(owner, shop, "SELECT count(*) FROM orders WHERE org_id = :o")


async def test_button_commits_before_the_model_speaks(
    shop: Shop, owner_engine: AsyncEngine, fake: FakeProvider
) -> None:
    async with await logged_in(shop, shop.x) as c:
        cid, pid = await propose_via_chat(c, fake)
        assert await order_count(owner_engine, shop) == 0
        fake.script += ["Your order is placed!"]  # text only: the model decides nothing
        r = await c.post(f"/api/conversations/{cid}/messages",
                         json={"action": {"type": "confirm_proposal", "proposal_id": pid}})  # fmt: skip

    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    kinds = [k for k, _ in events]
    placed = [d["card"] for k, d in events if k == "card" and d["card"]["type"] == "order_confirmed"]
    assert len(placed) == 1
    # The commit's card goes out before the first token of the model's reply.
    assert kinds.index("card") < kinds.index("token")
    assert await order_count(owner_engine, shop) == 1
    assert await stock(owner_engine, shop, "GRN-1") == 8
    # The model was told the outcome by the server, in the system prompt.
    last = fake.requests[-1]
    assert placed[0]["order_number"] in last.system
    assert len(fake.requests) == 3  # 2 for the proposing turn, 1 (text only) for this one
    # The model can't confirm again in this turn: the tool isn't even offered.
    assert "confirm_proposal" not in {t.name for t in last.tools}
    assert "confirm_proposal" in {t.name for t in fake.requests[0].tools}


async def test_button_commit_survives_a_model_failure(
    shop: Shop, owner_engine: AsyncEngine, fake: FakeProvider
) -> None:
    async with await logged_in(shop, shop.x) as c:
        cid, pid = await propose_via_chat(c, fake)
        fake.script += [ProviderUnavailable("all models down")]
        r = await c.post(f"/api/conversations/{cid}/messages",
                         json={"action": {"type": "confirm_proposal", "proposal_id": pid}})  # fmt: skip
    kinds = [k for k, _ in parse_sse(r.text)]
    assert "error" in kinds
    assert await order_count(owner_engine, shop) == 1  # committed regardless of the model


async def test_button_refusals_come_from_the_server(shop: Shop, owner_engine: AsyncEngine, fake: FakeProvider) -> None:
    async with await logged_in(shop, shop.x) as c:
        cid, pid = await propose_via_chat(c, fake)
        fake.script += ["Done.", "Already done."]
        for _ in range(2):  # double click
            await c.post(f"/api/conversations/{cid}/messages",
                         json={"action": {"type": "confirm_proposal", "proposal_id": pid}})  # fmt: skip
    assert await order_count(owner_engine, shop) == 1
    assert "already confirmed" in fake.requests[-1].system


async def test_button_for_someone_elses_proposal_is_not_found(
    shop: Shop, owner_engine: AsyncEngine, fake: FakeProvider
) -> None:
    async with await logged_in(shop, shop.x) as c:
        _, pid = await propose_via_chat(c, fake)
    async with await logged_in(shop, shop.y) as c:
        cid = (await c.post("/api/conversations")).json()["id"]
        fake.script += ["Sorry."]
        await c.post(f"/api/conversations/{cid}/messages",
                     json={"action": {"type": "confirm_proposal", "proposal_id": pid}})  # fmt: skip
    assert await order_count(owner_engine, shop) == 0
    assert "NOT FOUND" in fake.requests[-1].system


async def test_message_needs_content_or_action(shop: Shop, fake: FakeProvider) -> None:
    async with await logged_in(shop, shop.x) as c:
        cid = (await c.post("/api/conversations")).json()["id"]
        r = await c.post(f"/api/conversations/{cid}/messages", json={})
        bad = await c.post(f"/api/conversations/{cid}/messages",
                           json={"action": {"type": "delete_everything", "proposal_id": str(uuid.uuid4())}})  # fmt: skip
    assert r.status_code == 422 and bad.status_code == 422
