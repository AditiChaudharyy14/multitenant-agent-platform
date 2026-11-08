"""Gemini adapter against REAL recorded responses (tests/fixtures/llm/gemini, recorded with
`make record-fixtures`). Error bodies are hand-built from Google's documented format,
because recording a quota error would mean burning the quota."""

import pytest

from app.agents.providers.base import (
    ChatRequest,
    Finished,
    Message,
    ProviderAuthError,
    ProviderError,
    RateLimited,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolSpec,
)
from app.agents.providers.gemini import GeminiAdapter
from tests.agents.fixture_http import Replay, error, load

SEARCH = ToolSpec(
    name="search_knowledge",
    description="Search the shop's knowledge base for policies and product descriptions.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to search for"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)


async def run(replay: Replay, model: str, req: ChatRequest) -> list:
    adapter = GeminiAdapter(api_key="test-key", client=replay.client)
    return [e async for e in adapter.stream_once(model, req)]


async def test_text_stream() -> None:
    fx = load("gemini", "text")
    replay = Replay(fx)
    events = await run(replay, fx["model"], ChatRequest(system="", messages=[Message("user", "hi")]))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "Namaste" in text
    fin = events[-1]
    assert isinstance(fin, Finished) and fin.finish_reason == "stop" and fin.model == fx["model"]
    assert fin.usage.input_tokens > 0 and fin.usage.output_tokens > 0
    # The key travels in a header, never in the URL.
    req = replay.requests[0]
    assert req.headers["x-goog-api-key"] == "test-key"
    assert "key=" not in str(req.url)
    assert str(req.url).endswith(f"/models/{fx['model']}:streamGenerateContent?alt=sse")


async def test_tool_call_stream_keeps_thought_signature() -> None:
    fx = load("gemini", "tool_call")
    events = await run(
        Replay(fx), fx["model"], ChatRequest(system="s", messages=[Message("user", "q")], tools=[SEARCH])
    )
    calls = [e.call for e in events if isinstance(e, ToolCallEvent)]
    assert len(calls) == 1
    call = calls[0]
    assert call.name == "search_knowledge" and isinstance(call.arguments["query"], str)
    assert call.id == "call_0_0"  # deterministic: turn 0, first call
    assert call.provider_meta["model"] == fx["model"]
    assert call.provider_meta["thought_signature"]
    assert events[-1].finish_reason == "tool_calls"


def tool_round_trip(call: ToolCall) -> ChatRequest:
    return ChatRequest(
        system="You are a tea shop assistant.",
        messages=[
            Message("user", "What is your return policy for opened tea?"),
            Message("assistant", "", tool_calls=[call]),
            Message("tool", '{"answerable": true}', tool_call_id=call.id, name=call.name),
        ],
        tools=[SEARCH],
    )


async def test_request_translation_and_signature_round_trip() -> None:
    call_fx, result_fx = load("gemini", "tool_call"), load("gemini", "tool_result")
    call = next(
        e.call
        for e in await run(
            Replay(call_fx), call_fx["model"], ChatRequest(system="s", messages=[Message("user", "q")], tools=[SEARCH])
        )
        if isinstance(e, ToolCallEvent)
    )
    replay = Replay(result_fx)
    events = await run(replay, call_fx["model"], tool_round_trip(call))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)).strip()

    body = replay.body()
    assert body["systemInstruction"] == {"parts": [{"text": "You are a tea shop assistant."}]}
    decl = body["tools"][0]["functionDeclarations"][0]
    assert decl["name"] == "search_knowledge"
    assert "additionalProperties" not in decl["parameters"]  # unsupported by Gemini's schema subset
    user, model_turn, tool_turn = body["contents"]
    assert user["role"] == "user" and model_turn["role"] == "model" and tool_turn["role"] == "user"
    fc_part = model_turn["parts"][0]
    vendor_id = call.provider_meta["call_id"]  # Gemini 3 returns its own call id
    assert fc_part["functionCall"] == {"name": call.name, "args": call.arguments, "id": vendor_id}
    assert fc_part["thoughtSignature"] == call.provider_meta["thought_signature"]
    assert tool_turn["parts"][0]["functionResponse"] == {
        "name": call.name,
        "response": {"answerable": True},
        "id": vendor_id,
    }
    # The recorded follow-up (which Gemini answered with 200) carried the same shape.
    recorded = result_fx["request_body"]["contents"]
    assert recorded[1]["parts"][0]["functionCall"]["id"] == recorded[2]["parts"][0]["functionResponse"]["id"]


def test_signature_and_vendor_id_only_go_back_to_the_model_that_made_them() -> None:
    meta = {"model": "model-a", "thought_signature": "sig", "call_id": "vendor-1"}
    call = ToolCall("call_0_0", "search_knowledge", {"query": "x"}, meta)
    a = GeminiAdapter(api_key="k")
    same = a.build_body("model-a", tool_round_trip(call))["contents"]
    other = a.build_body("model-b", tool_round_trip(call))["contents"]
    assert same[1]["parts"][0]["thoughtSignature"] == "sig"
    assert same[1]["parts"][0]["functionCall"]["id"] == same[2]["parts"][0]["functionResponse"]["id"] == "vendor-1"
    assert "thoughtSignature" not in other[1]["parts"][0]
    assert "id" not in other[1]["parts"][0]["functionCall"] and "id" not in other[2]["parts"][0]["functionResponse"]


def test_consecutive_tool_results_share_one_turn() -> None:
    c1, c2 = ToolCall("call_0_0", "a", {}), ToolCall("call_0_1", "b", {})
    req = ChatRequest(system="", messages=[
        Message("user", "q"), Message("assistant", "", tool_calls=[c1, c2]),
        Message("tool", "[1, 2]", tool_call_id="call_0_0", name="a"),
        Message("tool", "not json", tool_call_id="call_0_1", name="b"),
    ])  # fmt: skip
    contents = GeminiAdapter(api_key="k").build_body("m", req)["contents"]
    assert len(contents) == 3
    parts = contents[2]["parts"]
    assert [p["functionResponse"]["response"] for p in parts] == [{"result": [1, 2]}, {"result": "not json"}]


QUOTA = "type.googleapis.com/google.rpc.QuotaFailure"
RETRY = "type.googleapis.com/google.rpc.RetryInfo"


def quota_429(quota_id: str, delay: str) -> dict:
    return error(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": [
        {"@type": QUOTA, "violations": [{"quotaId": quota_id, "quotaValue": "500"}]},
        {"@type": RETRY, "retryDelay": delay},
    ]}})  # fmt: skip


async def test_per_day_quota_429_is_daily() -> None:
    with pytest.raises(RateLimited) as ei:
        await run(
            Replay(quota_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "34s")),
            "m",
            ChatRequest(system="", messages=[Message("user", "q")]),
        )
    assert ei.value.daily is True


async def test_per_minute_quota_429_is_transient_with_retry_delay() -> None:
    with pytest.raises(RateLimited) as ei:
        await run(
            Replay(quota_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "17s")),
            "m",
            ChatRequest(system="", messages=[Message("user", "q")]),
        )
    assert ei.value.daily is False and ei.value.retry_after == 17.0


@pytest.mark.parametrize("status", [401, 403])
async def test_bad_key_is_an_auth_error(status: int) -> None:
    body = {"error": {"code": status, "status": "PERMISSION_DENIED", "message": "API key not valid"}}
    with pytest.raises(ProviderAuthError):
        await run(Replay(error(status, body)), "m", ChatRequest(system="", messages=[Message("user", "q")]))


async def test_other_errors_carry_status_and_message() -> None:
    body = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad schema"}}
    with pytest.raises(ProviderError) as ei:
        await run(Replay(error(400, body)), "m", ChatRequest(system="", messages=[Message("user", "q")]))
    assert "400" in str(ei.value) and "bad schema" in str(ei.value)


def test_tool_without_arguments_sends_no_parameters() -> None:
    """Gemini rejects an OBJECT schema with empty properties (e.g. list_my_orders)."""
    no_args = ToolSpec(
        "list_my_orders", "List orders.", {"type": "object", "properties": {}, "additionalProperties": False}
    )
    body = GeminiAdapter("k").build_body(
        "m", ChatRequest(system="s", messages=[Message("user", "q")], tools=[SEARCH, no_args])
    )
    with_args, without = body["tools"][0]["functionDeclarations"]
    assert "parameters" in with_args
    assert without == {"name": "list_my_orders", "description": "List orders."}


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_overload_is_transient_so_the_router_retries(status: int) -> None:
    """Seen in a live eval: 503 "This model is currently experiencing high demand" on a
    turn's second step became a user-facing error instead of a retry."""
    from app.agents.providers.base import ProviderOverloaded

    body = {
        "error": {
            "code": status,
            "status": "UNAVAILABLE",
            "message": "This model is currently experiencing high demand.",
        }
    }
    with pytest.raises(ProviderOverloaded) as ei:
        await run(Replay(error(status, body)), "m", ChatRequest(system="", messages=[Message("user", "q")]))
    assert isinstance(ei.value, RateLimited) and ei.value.daily is False
    assert str(status) in str(ei.value)
