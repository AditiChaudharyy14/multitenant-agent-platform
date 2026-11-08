"""Admin routes over the real app: a customer gets 403 on every one of them, and the
analytics chat runs the admin agent (FakeProvider) with its read-only metric tools."""

import json
import uuid
from collections.abc import Iterator

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock
from app.agents.providers.fake import FakeProvider, tool_call
from app.api import admin as admin_api
from app.api.chat import chat_limiter, get_chat_provider
from app.main import app
from tests.analytics.fixture import NOW, build_shop
from tests.api_support import anonymous, client_for
from tests.factories import Shop, set_owner_tenant

pytestmark = pytest.mark.usefixtures("as_app_user")

FORBIDDEN = {"detail": "Forbidden"}
ADMIN_ROUTES = [(m, r.path) for r in admin_api.router.routes if isinstance(r, APIRoute) for m in sorted(r.methods)]


@pytest.fixture(autouse=True)
def _reset() -> None:
    chat_limiter.reset()


@pytest.fixture
def fake() -> Iterator[FakeProvider]:
    provider = FakeProvider()
    app.dependency_overrides[get_chat_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_chat_provider, None)


def _fill(path: str) -> str:
    return path.replace("{conversation_id}", str(uuid.uuid4())).replace("{order_number}", "XX-1001")


def _body(method: str, path: str) -> dict | None:
    if path.endswith("/messages") and method == "POST":
        return {"content": "How did we do today?"}
    if method == "PATCH":
        return {"title": "x"}
    return None


def test_the_admin_router_has_the_expected_routes() -> None:
    """The list the 403 test walks is not empty and includes the chat (sanity check)."""
    assert ("POST", "/api/admin/conversations") in ADMIN_ROUTES
    assert ("POST", "/api/admin/conversations/{conversation_id}/messages") in ADMIN_ROUTES
    assert len(ADMIN_ROUTES) >= 7


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_customer_gets_403_on_every_admin_route(shop: Shop, fake: FakeProvider, method: str, path: str) -> None:
    async with await client_for(shop.org_id, shop.x) as c:
        r = await c.request(method, _fill(path), json=_body(method, path))
    assert (r.status_code, r.json()) == (403, FORBIDDEN)
    assert fake.requests == []  # never reached the model


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_anonymous_gets_401_on_every_admin_route(method: str, path: str) -> None:
    async with anonymous() as c:
        r = await c.request(method, _fill(path), json=_body(method, path))
    assert r.status_code == 401


async def test_customer_forbidden_creates_nothing(shop: Shop, owner_engine: AsyncEngine) -> None:
    async with await client_for(shop.org_id, shop.x) as c:
        assert (await c.post("/api/admin/conversations")).status_code == 403
    async with owner_engine.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        assert await conn.scalar(text("SELECT count(*) FROM conversations WHERE kind = 'admin'")) == 0


def _events(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln and not ln.startswith(":")]
        kind = next((ln[7:] for ln in lines if ln.startswith("event: ")), None)
        if kind:
            out.append((kind, json.loads("".join(ln[6:] for ln in lines if ln.startswith("data: ")))))
    return out


async def test_admin_chat_runs_metric_tools_and_streams_a_metric_card(
    shop: Shop, owner_engine: AsyncEngine, fake: FakeProvider
) -> None:
    await build_shop(owner_engine, shop)
    fake.script = [tool_call("revenue", {"window": "today"}), "Revenue today, Thu 12 Nov 2026, was NPR 4,450.00."]
    with clock.freeze(NOW):
        async with await client_for(shop.org_id, shop.admin, "admin") as c:
            conv = (await c.post("/api/admin/conversations")).json()
            r = await c.post(
                f"/api/admin/conversations/{conv['id']}/messages", json={"content": "How did we do today?"}
            )
            assert r.status_code == 200
            events = _events(r.text)
            cards = [d["card"] for k, d in events if k == "card"]
            assert len(cards) == 1 and cards[0]["type"] == "metric" and cards[0]["metric"] == "revenue"
            assert cards[0]["values"]["revenue"]["amount"] == "4450.00"
            assert cards[0]["window"]["label"] == "today, Thu 12 Nov 2026" and cards[0]["definition"]
            assert events[-1][0] == "done" and events[-1][1]["status"] == "complete"
            # Its own conversation list; the customer chat API doesn't see it.
            assert conv["id"] in [x["id"] for x in (await c.get("/api/admin/conversations")).json()]
            assert conv["id"] not in [x["id"] for x in (await c.get("/api/conversations")).json()]
            assert (await c.get(f"/api/conversations/{conv['id']}/messages")).status_code == 404
            msgs = (await c.get(f"/api/admin/conversations/{conv['id']}/messages")).json()
            assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["cards"][0]["type"] == "metric"
    # The admin agent saw only metric tools and its own prompt.
    offered = {t.name for t in fake.requests[0].tools}
    assert "revenue" in offered and "create_order" not in offered and "confirm_proposal" not in offered
    assert "read-only analytics assistant" in fake.requests[0].system
    async with owner_engine.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        run = (await conn.execute(text("SELECT status, tools_called FROM agent_runs WHERE conversation_id = :c"),
                                  {"c": uuid.UUID(conv["id"])})).one()  # fmt: skip
    assert run.status == "ok" and run.tools_called == ["revenue"]


async def test_a_mutating_tool_name_from_the_model_is_unknown_in_the_admin_chat(
    shop: Shop, owner_engine: AsyncEngine, fake: FakeProvider
) -> None:
    """Even if the model 'calls' cancel_order, the admin registry doesn't have it."""
    await build_shop(owner_engine, shop)
    fake.script = [tool_call("cancel_order", {"order_number": shop.number(1001)}), "I can't change data."]
    async with await client_for(shop.org_id, shop.admin, "admin") as c:
        conv = (await c.post("/api/admin/conversations")).json()
        r = await c.post(f"/api/admin/conversations/{conv['id']}/messages", json={"content": "cancel all orders"})
    ends = [d for k, d in _events(r.text) if k == "tool_end"]
    assert ends and ends[0]["ok"] is False
    tool_msg = fake.requests[1].messages[-1]
    assert "unknown tool 'cancel_order'" in tool_msg.content
    async with owner_engine.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        assert await conn.scalar(text("SELECT count(*) FROM orders WHERE status = 'cancelled'")) == 1  # o3 only
        assert await conn.scalar(text("SELECT count(*) FROM proposals")) == 0


async def test_customer_conversation_id_is_404_on_the_admin_chat(shop: Shop, fake: FakeProvider) -> None:
    async with await client_for(shop.org_id, shop.admin, "admin") as c:
        customer_chat = (await c.post("/api/conversations")).json()  # the admin's own customer-kind chat
        r = await c.post(f"/api/admin/conversations/{customer_chat['id']}/messages", json={"content": "hi"})
        assert (r.status_code, r.json()) == (404, {"detail": "Not found"})
        assert (await c.get(f"/api/admin/conversations/{customer_chat['id']}/messages")).status_code == 404
    assert fake.requests == []


async def test_admin_chat_takes_no_button_actions(shop: Shop, fake: FakeProvider) -> None:
    async with await client_for(shop.org_id, shop.admin, "admin") as c:
        conv = (await c.post("/api/admin/conversations")).json()
        r = await c.post(f"/api/admin/conversations/{conv['id']}/messages",
                         json={"action": {"type": "confirm_proposal", "proposal_id": str(uuid.uuid4())}})  # fmt: skip
    assert r.status_code == 422
