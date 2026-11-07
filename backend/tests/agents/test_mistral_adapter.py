"""Mistral adapter against HAND-BUILT fixtures that follow Mistral's documented SSE format.

There is no MISTRAL_API_KEY for this account (the free plan needs an upgrade), so nothing
could be recorded; tests_live/ has the live check, skipped while the key is absent.
"""

import json
import re

import pytest

from app.agents.providers.base import (
    ChatRequest,
    Finished,
    Message,
    ProviderAuthError,
    RateLimited,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolSpec,
)
from app.agents.providers.mistral import MistralAdapter
from tests.agents.fixture_http import Replay, error, load

SEARCH = ToolSpec("search_knowledge", "Search the KB.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})


async def run(replay: Replay, req: ChatRequest, model: str = "mistral-small-latest") -> list:
    adapter = MistralAdapter(api_key="test-key", client=replay.client)
    return [e async for e in adapter.stream_once(model, req)]


async def test_text_stream_and_usage() -> None:
    replay = Replay(load("mistral", "text"))
    events = await run(replay, ChatRequest(system="Be brief.", messages=[Message("user", "hi")]))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Namaste! How can I help?"
    assert events[-1] == Finished("stop", events[-1].usage, "mistral-small-latest")
    assert (events[-1].usage.input_tokens, events[-1].usage.output_tokens) == (21, 7)
    req = replay.requests[0]
    assert req.headers["authorization"] == "Bearer test-key"
    assert str(req.url) == "https://api.mistral.ai/v1/chat/completions"
    body = replay.body()
    assert body["stream"] is True and body["model"] == "mistral-small-latest"
    assert body["messages"][0] == {"role": "system", "content": "Be brief."}


async def test_fragmented_tool_call_arguments_are_assembled() -> None:
    events = await run(Replay(load("mistral", "tool_call")), ChatRequest(system="", messages=[Message("user", "q")], tools=[SEARCH]))
    calls = [e.call for e in events if isinstance(e, ToolCallEvent)]
    assert calls == [ToolCall("D681PevKs", "search_knowledge", {"query": "opened tea returns"}, None)]
    assert events[-1].finish_reason == "tool_calls"


def test_request_translation() -> None:
    call = ToolCall("D681PevKs", "search_knowledge", {"query": "x"})
    foreign = ToolCall("call_0_1", "search_knowledge", {"query": "y"})  # e.g. made by Gemini earlier
    req = ChatRequest(system="sys", messages=[
        Message("user", "q"),
        Message("assistant", "", tool_calls=[call, foreign]),
        Message("tool", '{"ok": true}', tool_call_id="D681PevKs", name="search_knowledge"),
        Message("tool", '{"ok": false}', tool_call_id="call_0_1", name="search_knowledge"),
    ], tools=[SEARCH], temperature=0.1, max_output_tokens=300)  # fmt: skip
    body = MistralAdapter(api_key="k").build_body("m", req)
    assert body["tools"] == [{"type": "function", "function": {"name": "search_knowledge", "description": "Search the KB.", "parameters": SEARCH.parameters}}]
    assert (body["temperature"], body["max_tokens"]) == (0.1, 300)
    asst = body["messages"][2]
    assert asst["tool_calls"][0] == {"id": "D681PevKs", "type": "function", "function": {"name": "search_knowledge", "arguments": json.dumps({"query": "x"})}}
    # Mistral requires 9-character alphanumeric ids: foreign ids are mapped, consistently.
    mapped = asst["tool_calls"][1]["id"]
    assert re.fullmatch(r"[A-Za-z0-9]{9}", mapped)
    assert body["messages"][4] == {"role": "tool", "content": '{"ok": false}', "tool_call_id": mapped, "name": "search_knowledge"}


async def test_429_is_rate_limited_with_retry_after() -> None:
    body = {"object": "error", "message": "Requests rate limit exceeded", "type": "rate_limited", "code": "1300"}
    with pytest.raises(RateLimited) as ei:
        await run(Replay(error(429, body, {"retry-after": "2"})), ChatRequest(system="", messages=[Message("user", "q")]))
    assert ei.value.retry_after == 2.0 and ei.value.daily is False


async def test_401_is_auth_error() -> None:
    with pytest.raises(ProviderAuthError):
        await run(Replay(error(401, {"message": "Unauthorized"})), ChatRequest(system="", messages=[Message("user", "q")]))


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_overload_is_transient(status: int) -> None:
    from app.agents.providers.base import ProviderOverloaded

    with pytest.raises(ProviderOverloaded) as ei:
        await run(Replay(error(status, {"message": "Service unavailable"})), ChatRequest(system="", messages=[Message("user", "q")]))
    assert isinstance(ei.value, RateLimited) and ei.value.daily is False
