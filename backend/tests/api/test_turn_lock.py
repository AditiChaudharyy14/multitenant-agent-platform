"""One reply at a time per conversation: a second message while a reply is still being
written gets 409 "A reply is still in progress" (a Postgres advisory lock per
conversation, held for the whole turn and released when it ends)."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from app.agents.providers.base import ChatRequest, Event, Finished, LLMProvider, TextDelta, Usage
from app.api.chat import get_chat_provider
from app.db.session import app_engine
from app.main import app
from tests.api_support import client_for
from tests.factories import Shop

pytestmark = pytest.mark.usefixtures("as_app_user")


class GatedProvider(LLMProvider):
    """Answers only once `gate` is set, so a turn can be held open mid-reply."""

    name = "gated"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def stream(self, req: ChatRequest) -> AsyncIterator[Event]:
        self.started.set()
        await self.gate.wait()
        yield TextDelta("Done.")
        yield Finished("stop", Usage(1, 1), "gated-model")


async def advisory_locks() -> int:
    async with app_engine.connect() as conn:
        return await conn.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))


async def test_a_second_message_while_replying_is_409_and_the_lock_is_released(shop: Shop) -> None:
    provider = GatedProvider()
    app.dependency_overrides[get_chat_provider] = lambda: provider
    try:
        async with await client_for(shop.org_id, shop.x) as c:
            cid = (await c.post("/api/conversations")).json()["id"]
            other = (await c.post("/api/conversations")).json()["id"]
            first = asyncio.create_task(c.post(f"/api/conversations/{cid}/messages", json={"content": "hello"}))
            await asyncio.wait_for(provider.started.wait(), 10)  # the first reply is being written

            busy = await c.post(f"/api/conversations/{cid}/messages", json={"content": "are you there?"})
            assert busy.status_code == 409
            assert busy.json() == {"detail": "A reply is still in progress"}

            provider.gate.set()
            done = await asyncio.wait_for(first, 20)
            assert done.status_code == 200 and "event: done" in done.text

            again = await c.post(f"/api/conversations/{cid}/messages", json={"content": "thanks"})
            assert again.status_code == 200  # released: the next message goes through
            assert (await c.post(f"/api/conversations/{other}/messages", json={"content": "hi"})).status_code == 200
        # Only the second message of the busy pair was refused; it was never stored.
        async with await client_for(shop.org_id, shop.x) as c:
            texts = [
                m["content"] for m in (await c.get(f"/api/conversations/{cid}/messages")).json() if m["role"] == "user"
            ]
        assert texts == ["hello", "thanks"]
        assert await advisory_locks() == 0  # nothing left locked in the pool
    finally:
        provider.gate.set()
        app.dependency_overrides.pop(get_chat_provider, None)


async def test_two_conversations_do_not_block_each_other(shop: Shop) -> None:
    provider = GatedProvider()
    app.dependency_overrides[get_chat_provider] = lambda: provider
    try:
        async with await client_for(shop.org_id, shop.x) as c:
            a = (await c.post("/api/conversations")).json()["id"]
            b = (await c.post("/api/conversations")).json()["id"]
            first = asyncio.create_task(c.post(f"/api/conversations/{a}/messages", json={"content": "one"}))
            await asyncio.wait_for(provider.started.wait(), 10)
            second = asyncio.create_task(c.post(f"/api/conversations/{b}/messages", json={"content": "two"}))
            await asyncio.sleep(0.5)
            provider.gate.set()
            assert (await asyncio.wait_for(first, 20)).status_code == 200
            assert (await asyncio.wait_for(second, 20)).status_code == 200
    finally:
        provider.gate.set()
        app.dependency_overrides.pop(get_chat_provider, None)
