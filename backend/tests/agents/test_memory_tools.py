"""Memory tools, the org `features` flag, the "What I remember" panel API, and the block the
turn adds to the system prompt. As app_user throughout; the FakeProvider never calls out.
"""

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.providers.base import ChatRequest
from app.agents.tools.common.memory import MEMORY_TOOLS, forget_preference, get_my_preferences, remember_preference
from app.agents.tools.registry import REGISTRY, tools_for
from app.db.session import tenant_tx
from app.domain import memory
from app.tenancy.orgs import OrgConfig
from tests.api_support import anonymous, client_for
from tests.factories import Shop, Studio, insert_shop_order, set_owner_tenant
from tests.store_support import call, turn_ctx

pytestmark = pytest.mark.usefixtures("as_app_user")

NAMES = {"remember_preference", "forget_preference", "get_my_preferences"}


# ───────────── registry and feature flag ─────────────


@pytest.mark.parametrize("vertical", list(REGISTRY))
def test_memory_tools_are_on_by_default_for_every_vertical(vertical: str) -> None:
    assert NAMES <= set(tools_for(vertical))
    assert NAMES <= set(tools_for(vertical, {"memory": True}))


@pytest.mark.parametrize("vertical", list(REGISTRY))
def test_memory_feature_off_removes_the_tools(vertical: str) -> None:
    assert not NAMES & set(tools_for(vertical, {"memory": False}))


def test_org_config_features_default_on_and_reject_unknown() -> None:
    base = {"slug": "x", "name": "X", "vertical": "store", "timezone": "Asia/Kathmandu", "order_prefix": "XQ",
            "branding": {"primary_color": "#000000", "secondary_color": "#ffffff", "logo_emoji": "x",
                         "agent_name": "A", "greeting": "hi"}}  # fmt: skip
    assert OrgConfig.model_validate(base).features.memory is True
    assert OrgConfig.model_validate({**base, "features": {"memory": False}}).features.memory is False
    with pytest.raises(ValueError):
        OrgConfig.model_validate({**base, "features": {"telepathy": True}})


def test_memory_tool_schemas_carry_no_identity() -> None:
    for tool in MEMORY_TOOLS:
        props = set(tool.spec().parameters.get("properties", {}))
        assert not props & {"org_id", "user_id", "customer_id", "role"}, tool.name
        with pytest.raises(ValueError):
            tool.args_model.model_validate({"user_id": str(uuid.uuid4())})


# ───────────── the tools ─────────────


async def test_remember_get_forget_round_trip(shop: Shop, owner_engine: AsyncEngine) -> None:
    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-2", 3)], status="delivered")
    ctx = turn_ctx(shop, shop.x)
    r = await call(remember_preference, ctx, key="milk", value="oat milk please")
    assert r.ok, r.content_for_model
    got = json.loads((await call(get_my_preferences, ctx)).content_for_model)
    by = {m["key"]: m for m in got["memories"]}
    assert by["milk"] == {"key": "milk", "value": "oat milk please", "source": "stated"}
    assert by["usual_products"]["source"] == "derived" and "Green Beta (SKU GRN-2)" in by["usual_products"]["value"]
    assert (await call(forget_preference, ctx, key="milk")).ok
    got = json.loads((await call(get_my_preferences, ctx)).content_for_model)
    assert "milk" not in {m["key"] for m in got["memories"]}


async def test_a_refused_memory_is_an_answer_not_a_crash(shop: Shop) -> None:
    r = await call(remember_preference, turn_ctx(shop, shop.x), key="usual_products", value="all of it")
    assert not r.ok and "derived" in r.content_for_model


async def test_get_my_preferences_never_shows_another_customer(shop: Shop) -> None:
    await call(remember_preference, turn_ctx(shop, shop.y), key="nickname", value="Yeti")
    got = json.loads((await call(get_my_preferences, turn_ctx(shop, shop.x))).content_for_model)
    assert got["memories"] == []


# ───────────── panel API ─────────────


async def test_memories_endpoint_lists_and_deletes_own(shop: Shop, owner_engine: AsyncEngine) -> None:
    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-1", 1)], status="delivered")
    await call(remember_preference, turn_ctx(shop, shop.x), key="milk", value="oat")
    async with await client_for(shop.org_id, shop.x) as c:
        r = await c.get("/api/memories")
        assert r.status_code == 200
        keys = {m["key"]: m["source"] for m in r.json()["memories"]}
        assert keys == {"milk": "stated", "usual_products": "derived"}
        assert (await c.delete("/api/memories/milk")).status_code == 204
        assert (await c.delete("/api/memories/usual_products")).status_code == 204
        assert (await c.get("/api/memories")).json()["memories"] == []


async def test_deleting_a_missing_or_foreign_memory_is_the_same_404(shop: Shop) -> None:
    await call(remember_preference, turn_ctx(shop, shop.y), key="nickname", value="Yeti")
    async with await client_for(shop.org_id, shop.x) as c:
        foreign = await c.delete("/api/memories/nickname")
        missing = await c.delete("/api/memories/no_such_key")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.content == missing.content
    async with await client_for(shop.org_id, shop.y) as c:  # Y's memory survived
        assert [m["key"] for m in (await c.get("/api/memories")).json()["memories"]] == ["nickname"]


async def test_memories_endpoint_needs_a_session() -> None:
    async with anonymous() as c:
        assert (await c.get("/api/memories")).status_code == 401


async def test_feature_off_hides_the_panel_with_404(shop: Shop, owner_engine: AsyncEngine) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(text("UPDATE orgs SET features = '{\"memory\": false}' WHERE id = :o"), {"o": shop.org_id})
    async with await client_for(shop.org_id, shop.x) as c:
        assert (await c.get("/api/memories")).status_code == 404


# ───────────── the turn: block in the system prompt, new conversation ─────────────


async def test_usual_order_in_a_brand_new_conversation(studio: Studio, shop: Shop, owner_engine: AsyncEngine) -> None:
    """'My usual' works in a conversation with no history: the facts come from the block the
    server adds at turn start, built from this customer's rows only."""
    from tests.agents.turn_support import run_customer_turn  # the FakeProvider turn harness

    await insert_shop_order(owner_engine, shop, shop.x, [("GRN-3", 2)], status="delivered")
    await call(remember_preference, turn_ctx(shop, shop.y), key="nickname", value="Yeti")
    new_conv = await _new_conversation(owner_engine, shop, shop.x)
    seen: list[ChatRequest] = []
    events = await run_customer_turn(shop, shop.x, new_conv, "my usual tea please", seen=seen,
                                     script=[{"tool": "create_order", "args": {
                                         "items": [{"sku": "GRN-3", "qty": 2}], "delivery_address": "House 1"}},
                                         {"text": "Here is your usual."}])  # fmt: skip
    system = seen[0].system
    assert "<customer_memory>" in system and "Green Gamma (SKU GRN-3)" in system
    assert "Yeti" not in system
    assert any(e.type == "card" and e.data["card"]["type"] == "order_summary" for e in events)


async def test_admin_turns_get_no_memory_block(shop: Shop) -> None:
    from tests.agents.turn_support import system_prompt_for

    assert "<customer_memory>" not in await system_prompt_for(shop, shop.admin, agent="admin")
    assert "<customer_memory>" in await system_prompt_for(shop, shop.x, agent="customer")


async def _new_conversation(owner: AsyncEngine, shop: Shop, user: uuid.UUID) -> uuid.UUID:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        return await conn.scalar(text("INSERT INTO conversations (org_id, user_id) VALUES (:o, :u) RETURNING id"),
                                 {"o": shop.org_id, "u": user})  # fmt: skip


async def test_memory_domain_is_the_tools_source(shop: Shop) -> None:
    """The panel and the tool share memory.list_memories: they can't disagree."""
    ctx = turn_ctx(shop, shop.x)
    await call(remember_preference, ctx, key="milk", value="oat")
    async with tenant_tx(ctx.tenant) as conn:
        direct = [m.as_dict() for m in await memory.list_memories(conn, ctx, "store")]
    assert json.loads((await call(get_my_preferences, ctx)).content_for_model)["memories"] == direct
