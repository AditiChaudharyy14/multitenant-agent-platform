"""Provider selection, the FakeProvider, and the guarantee that `make test` is offline."""

import pytest

from app.agents.providers import get_provider
from app.agents.providers.base import ChatRequest, Finished, Message, ProviderAuthError, TextDelta, ToolCallEvent
from app.agents.providers.fake import FakeProvider, tool_call
from app.agents.providers.recording import RecordReplayProvider
from app.agents.providers.router import ModelRouter
from app.config import Settings, get_settings


def settings(**overrides: object) -> Settings:
    return get_settings().model_copy(update=overrides)


def test_tests_run_on_the_fake_provider() -> None:
    assert get_settings().llm_provider == "fake"
    assert isinstance(get_provider(), FakeProvider)


def test_default_provider_is_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER")
    s = Settings()
    assert s.llm_provider == "gemini"
    assert s.llm_models("gemini") == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    assert (s.llm_rpm_gemini, s.llm_daily_budget_gemini) == (12, 450)


@pytest.mark.parametrize("provider", ["gemini", "mistral"])
def test_missing_key_fails_with_a_clear_message(provider: str) -> None:
    with pytest.raises(ProviderAuthError) as ei:
        get_provider(settings(llm_provider=provider))
    assert f"{provider.upper()}_API_KEY" in str(ei.value)


def test_live_provider_is_a_model_router() -> None:
    from pydantic import SecretStr

    p = get_provider(settings(llm_provider="gemini", gemini_api_key=SecretStr("k")))
    assert isinstance(p, ModelRouter)
    assert p.models == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]


def test_replay_needs_no_key(tmp_path) -> None:  # noqa: ANN001
    p = get_provider(settings(llm_provider="gemini", eval_mode="replay", eval_cassette_dir=str(tmp_path)))
    assert isinstance(p, RecordReplayProvider)


async def test_fake_provider_scripts_turns_and_records_requests() -> None:
    fake = FakeProvider([tool_call("search_knowledge", {"query": "q"}), "Hello there friend."])
    r1 = ChatRequest(system="s", messages=[Message("user", "a")])
    events = [e async for e in fake.stream(r1)]
    assert isinstance(events[0], ToolCallEvent) and events[-1].finish_reason == "tool_calls"
    events = [e async for e in fake.stream(r1)]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello there friend."
    assert len([e for e in events if isinstance(e, TextDelta)]) > 1  # really streamed
    assert isinstance(events[-1], Finished)
    assert fake.requests == [r1, r1]
    with pytest.raises(AssertionError):
        [e async for e in fake.stream(r1)]  # script exhausted: a test asked for more than it scripted
