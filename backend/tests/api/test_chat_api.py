"""Conversations and SSE chat over the real app (ASGI), as app_user, on FakeProvider.

Each test gets fresh orgs: copies of the seed org configs under unique slugs (picked by
vertical, never by slug), with the real store KB chunked and embedded by the seeding code,
so search_knowledge runs for real. The seeded orgs themselves are never touched, so the
seed idempotency test keeps seeing pure seed data.
"""

import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.providers.base import ChatRequest, ProviderUnavailable
from app.agents.providers.fake import FakeProvider, tool_call
from app.api.chat import chat_limiter, get_chat_provider
from app.auth.ratelimit import login_limiter
from app.config import get_settings, require_owner_url
from app.domain.kb.ingest import PreparedKB, insert_chunks, prepare_kb
from app.main import app
from app.seed.data import load_seed_data
from app.tenancy.orgs import OrgConfig, sync_orgs
from tests.factories import set_owner_tenant, unique_prefix

pytestmark = pytest.mark.usefixtures("as_app_user")

_KB_CACHE: dict[str, PreparedKB] = {}  # embedding the KB is the slow part: once per run


async def _store_kb(cfg: OrgConfig) -> PreparedKB:
    if "store" not in _KB_CACHE:
        org_dir = Path(get_settings().seed_dir) / cfg.slug
        data = load_seed_data(org_dir / "data.yaml", cfg.vertical)
        catalogs = {"products": {p.name: p.sku for p in data.products},
                    "services": {s.name: None for s in data.services}}  # fmt: skip
        _KB_CACHE["store"] = await prepare_kb(org_dir / "kb", catalogs)
    return _KB_CACHE["store"]


@pytest.fixture
async def chat_orgs(demo_org_configs: list[OrgConfig], owner_engine: AsyncEngine) -> dict[str, str]:
    """vertical → slug of a fresh org for this test; the store one has the real KB."""
    # Fresh order and booking prefixes too: both are unique in orgs, like the slug.
    copies = {c.vertical: c.model_copy(update={"slug": f"chat-{c.vertical}-{uuid.uuid4().hex[:10]}",
                                               "order_prefix": unique_prefix() if c.order_prefix else None,
                                               "booking_prefix": unique_prefix() if c.booking_prefix else None})
              for c in demo_org_configs}  # fmt: skip
    await sync_orgs(require_owner_url(), list(copies.values()))
    kb = await _store_kb(next(c for c in demo_org_configs if c.vertical == "store"))
    async with owner_engine.begin() as conn:
        org_id = await conn.scalar(text("SELECT id FROM orgs WHERE slug = :s"), {"s": copies["store"].slug})
        await set_owner_tenant(conn, org_id)
        await insert_chunks(conn, org_id, kb)
    return {v: c.slug for v, c in copies.items()}


ORIGIN = "http://localhost:3000"
NOT_FOUND = {"detail": "Not found"}
Client = Callable[[str | None], httpx.AsyncClient]


@pytest.fixture(autouse=True)
def _reset_limiters() -> None:
    chat_limiter.reset()
    login_limiter.reset()


@pytest.fixture
def fake() -> AsyncIterator[FakeProvider]:
    provider = FakeProvider()
    app.dependency_overrides[get_chat_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_chat_provider, None)


@pytest.fixture
def client() -> Client:
    def make(sid: str | None = None) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                              headers={"Origin": ORIGIN}, timeout=30)  # fmt: skip
        if sid:
            c.cookies.set("sid", sid)
        return c

    return make


async def signup(client: Client, slug: str) -> str:
    email = f"chat-{uuid.uuid4().hex[:12]}@example.test"
    async with client(None) as c:
        r = await c.post(f"/api/orgs/{slug}/auth/signup",
                         json={"email": email, "password": "correct horse battery", "display_name": "Asha"})  # fmt: skip
    assert r.status_code == 201, r.text
    return r.cookies["sid"]


@pytest.fixture
async def store_sid(client: Client, chat_orgs: dict[str, str]) -> str:
    return await signup(client, chat_orgs["store"])


async def new_conversation(c: httpx.AsyncClient) -> str:
    r = await c.post("/api/conversations")
    assert r.status_code == 201, r.text
    return r.json()["id"]


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln and not ln.startswith(":")]
        if not lines:
            continue
        kind = next(ln[7:] for ln in lines if ln.startswith("event: "))
        data = "\n".join(ln[6:] for ln in lines if ln.startswith("data: "))
        events.append((kind, json.loads(data)))
    return events


def cite_first_source(req: ChatRequest) -> str:
    """A scripted model that cites whatever search_knowledge actually returned."""
    source = re.search(r'<kb_source id="([^"]+)"', req.messages[-1].content)
    assert source, req.messages[-1].content
    return f"Unopened items can be returned [{source.group(1)}]. Also [made-up#section]."


async def test_conversation_crud_and_auto_title(client: Client, store_sid: str, fake: FakeProvider) -> None:
    fake.script += ["Sure.", "Again."]
    async with client(store_sid) as c:
        cid = await new_conversation(c)
        listed = (await c.get("/api/conversations")).json()
        assert [x["id"] for x in listed] == [cid] and listed[0]["title"] == "New chat"

        await c.post(f"/api/conversations/{cid}/messages",
                     json={"content": "What is your   return policy for opened tea please"})  # fmt: skip
        await c.post(f"/api/conversations/{cid}/messages", json={"content": "And for teaware?"})
        assert (await c.get("/api/conversations")).json()[0]["title"] == "What is your return policy for"

        r = await c.patch(f"/api/conversations/{cid}", json={"title": "Returns"})
        assert r.status_code == 200 and r.json()["title"] == "Returns"
        assert (await c.patch(f"/api/conversations/{cid}", json={"title": ""})).status_code == 422

        msgs = (await c.get(f"/api/conversations/{cid}/messages")).json()
        assert [(m["role"], m["status"]) for m in msgs] == [
            ("user", "complete"),
            ("assistant", "complete"),
            ("user", "complete"),
            ("assistant", "complete"),
        ]

        assert (await c.delete(f"/api/conversations/{cid}")).status_code == 204
        assert (await c.get("/api/conversations")).json() == []
        r = await c.get(f"/api/conversations/{cid}/messages")
        assert (r.status_code, r.json()) == (404, NOT_FOUND)


async def test_sse_turn_with_kb_search_and_citations(
    client: Client, store_sid: str, fake: FakeProvider, owner_engine: AsyncEngine, chat_orgs: dict[str, str]
) -> None:
    fake.script += [tool_call("search_knowledge", {"query": "return policy unopened tea"}, id="c1"), cite_first_source]
    async with client(store_sid) as c:
        cid = await new_conversation(c)
        r = await c.post(f"/api/conversations/{cid}/messages", json={"content": "What is your return policy?"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-accel-buffering"] == "no"
    assert r.headers["cache-control"] == "no-cache"

    events = parse_sse(r.text)
    kinds = [k for i, (k, _) in enumerate(events) if i == 0 or events[i - 1][0] != k]
    assert kinds == ["tool_start", "tool_end", "token", "citation", "done"]
    tool_start = events[0][1]
    assert tool_start["name"] == "search_knowledge" and tool_start["label"]
    citations = [d["id"] for k, d in events if k == "citation"]
    assert len(citations) == 1 and citations[0] != "made-up#section"
    done = events[-1][1]
    assert done["status"] == "complete"
    assert "made-up#section" not in done["content"]
    assert [x["id"] for x in done["citations"]] == citations

    # Identity in the model's request comes only from the server: no ids leak into the prompt.
    assert "org_id" not in json.dumps(fake.requests[0].tools[0].parameters)

    async with client(store_sid) as c:
        msgs = (await c.get(f"/api/conversations/{cid}/messages")).json()
    assistant = msgs[-1]
    assert assistant["id"] == done["message_id"]
    assert msgs[0]["id"] == done["turn_id"]  # the user message id is the turn id
    assert [x["id"] for x in assistant["citations"]] == citations
    assert assistant["tool_calls"][0]["calls"][0]["name"] == "search_knowledge"

    async with owner_engine.begin() as conn:
        org_id = await conn.scalar(text("SELECT id FROM orgs WHERE slug = :s"), {"s": chat_orgs["store"]})
        await set_owner_tenant(conn, org_id)
        run = (await conn.execute(text("SELECT provider, model, status, tools_called FROM agent_runs "
                                       "WHERE conversation_id = :c"), {"c": cid})).one()  # fmt: skip
    assert tuple(run) == ("fake", "fake-model", "ok", ["search_knowledge"])


async def test_history_is_replayed_on_the_next_turn(client: Client, store_sid: str, fake: FakeProvider) -> None:
    fake.script += [tool_call("search_knowledge", {"query": "returns"}, id="c1"), cite_first_source, "Yes."]
    async with client(store_sid) as c:
        cid = await new_conversation(c)
        await c.post(f"/api/conversations/{cid}/messages", json={"content": "returns?"})
        await c.post(f"/api/conversations/{cid}/messages", json={"content": "thanks"})
    roles = [m.role for m in fake.requests[-1].messages]
    assert roles == ["user", "assistant", "tool", "assistant", "user"]
    tool_msg = fake.requests[-1].messages[2]
    assert tool_msg.tool_call_id == "c1" and len(tool_msg.content) <= 1500


async def test_provider_failure_streams_error_then_done(client: Client, store_sid: str, fake: FakeProvider) -> None:
    fake.script += [ProviderUnavailable("every model is rate limited")]
    async with client(store_sid) as c:
        cid = await new_conversation(c)
        r = await c.post(f"/api/conversations/{cid}/messages", json={"content": "hello"})
        msgs = (await c.get(f"/api/conversations/{cid}/messages")).json()
    events = parse_sse(r.text)
    assert [k for k, _ in events] == ["error", "done"]
    assert events[0][1]["code"] == "rate_limited" and events[0][1]["message"]
    assert events[1][1]["status"] == "error"
    assert [(m["role"], m["status"]) for m in msgs] == [("user", "complete"), ("assistant", "error")]


async def test_other_users_and_orgs_conversations_are_404(
    client: Client, chat_orgs: dict[str, str], fake: FakeProvider
) -> None:
    owner = await signup(client, chat_orgs["store"])
    same_org = await signup(client, chat_orgs["store"])
    other_org = await signup(client, chat_orgs["booking"])
    async with client(owner) as c:
        cid = await new_conversation(c)

    for sid in (same_org, other_org):
        async with client(sid) as c:
            responses = [
                await c.get(f"/api/conversations/{cid}/messages"),
                await c.patch(f"/api/conversations/{cid}", json={"title": "mine now"}),
                await c.delete(f"/api/conversations/{cid}"),
                await c.post(f"/api/conversations/{cid}/messages", json={"content": "hi"}),
            ]
            assert [x["id"] for x in (await c.get("/api/conversations")).json()] == []
        for r in responses:
            assert (r.status_code, r.json()) == (404, NOT_FOUND), r.request.url
    # A random id gives the very same answer: existence is never revealed.
    async with client(owner) as c:
        r = await c.get(f"/api/conversations/{uuid.uuid4()}/messages")
        assert (r.status_code, r.json()) == (404, NOT_FOUND)
        assert (await c.get("/api/conversations")).json()[0]["title"] == "New chat"  # untouched
    assert fake.requests == []  # no turn ran for anyone


async def test_21st_message_in_a_minute_is_429(client: Client, store_sid: str, fake: FakeProvider) -> None:
    fake.script += ["ok"] * 20
    async with client(store_sid) as c:
        cid = await new_conversation(c)
        for i in range(20):
            r = await c.post(f"/api/conversations/{cid}/messages", json={"content": f"message {i}"})
            assert r.status_code == 200, (i, r.text)
        r = await c.post(f"/api/conversations/{cid}/messages", json={"content": "one too many"})
        assert r.status_code == 429
        assert 1 <= int(r.headers["retry-after"]) <= 60
        msgs = (await c.get(f"/api/conversations/{cid}/messages")).json()
    assert len(msgs) == 40  # the refused message was never stored
    assert fake.script == []


async def test_chat_requires_a_session(client: Client) -> None:
    async with client(None) as c:
        assert (await c.get("/api/conversations")).status_code == 401
        assert (await c.post("/api/conversations")).status_code == 401


async def test_message_body_is_validated(client: Client, store_sid: str, fake: FakeProvider) -> None:
    async with client(store_sid) as c:
        cid = await new_conversation(c)
        assert (await c.post(f"/api/conversations/{cid}/messages", json={"content": ""})).status_code == 422
        assert (await c.post(f"/api/conversations/{cid}/messages", json={"content": "x" * 4001})).status_code == 422
        r = await c.post(f"/api/conversations/{cid}/messages",
                         json={"content": "hi", "org_id": str(uuid.uuid4())})  # fmt: skip
        assert r.status_code == 422  # unknown fields are rejected, never read


async def test_e2e_scenario_cites_the_real_kb_through_the_api(client: Client, store_sid: str) -> None:
    """The Playwright scenario, end to end in-process: the scripted model calls the real
    search_knowledge, and the citation it fills in survives the server-side filter."""
    from app.agents.providers.scripted import ScriptedProvider, load_scenario

    scenario = load_scenario(Path(__file__).resolve().parents[2] / "fake_scenarios" / "e2e.yaml")
    app.dependency_overrides[get_chat_provider] = lambda: ScriptedProvider(scenario)
    try:
        async with client(store_sid) as c:
            cid = await new_conversation(c)
            r = await c.post(f"/api/conversations/{cid}/messages", json={"content": "How long does delivery take?"})
    finally:
        app.dependency_overrides.pop(get_chat_provider, None)
    events = parse_sse(r.text)
    done = events[-1][1]
    assert done["status"] == "complete"
    assert any(k == "tool_start" and d["name"] == "search_knowledge" for k, d in events)
    cited = [d["id"] for k, d in events if k == "citation"]
    assert cited and cited == [x["id"] for x in done["citations"]]
    assert f"[{cited[0]}]" in done["content"]
