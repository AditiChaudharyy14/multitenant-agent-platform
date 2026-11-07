"""EVAL_MODE=record|replay: record real responses once, replay them for free."""

import json
import uuid
from pathlib import Path

import pytest

from app.agents.providers.base import ChatRequest, Finished, Message, TextDelta, ToolCallEvent
from app.agents.providers.fake import FakeProvider, tool_call
from app.agents.providers.recording import CassetteMismatch, CassetteMissing, RecordReplayProvider, cassette_scope


def req(text: str = "hi") -> ChatRequest:
    return ChatRequest(system="s", messages=[Message("user", text)])


async def collect(p, r: ChatRequest) -> list:  # noqa: ANN001
    return [e async for e in p.stream(r)]


async def test_record_then_replay_gives_identical_events(tmp_path: Path) -> None:
    inner = FakeProvider([tool_call("search_knowledge", {"query": "returns"}), "Unopened tea can be returned."])
    rec = RecordReplayProvider("record", tmp_path, inner)
    with cassette_scope("store/returns"):
        first = await collect(rec, req())
        second = await collect(rec, req("follow-up"))
    assert (tmp_path / "store" / "returns.json").exists()

    replay = RecordReplayProvider("replay", tmp_path)  # no inner provider: no network, no budget
    with cassette_scope("store/returns"):
        assert await collect(replay, req()) == first
        assert await collect(replay, req("follow-up")) == second
    assert isinstance(first[0], ToolCallEvent) and isinstance(second[-1], Finished)
    assert len(inner.requests) == 2  # replay never reached the inner provider


async def test_rerecording_a_scenario_starts_it_afresh(tmp_path: Path) -> None:
    with cassette_scope("s"):
        await collect(RecordReplayProvider("record", tmp_path, FakeProvider(["a"])), req())
        await collect(RecordReplayProvider("record", tmp_path, FakeProvider(["b"])), req("2"))
    with cassette_scope("s"):
        await collect(RecordReplayProvider("record", tmp_path, FakeProvider(["c"])), req())
    calls = json.loads((tmp_path / "s.json").read_text())["calls"]
    assert len(calls) == 1


async def test_missing_recording_says_how_to_record(tmp_path: Path) -> None:
    replay = RecordReplayProvider("replay", tmp_path)
    with cassette_scope("booking/never_recorded"), pytest.raises(CassetteMissing) as ei:
        await collect(replay, req())
    assert "booking/never_recorded" in str(ei.value) and "EVAL_MODE=record" in str(ei.value)


async def test_changed_request_is_a_mismatch_not_a_stale_replay(tmp_path: Path) -> None:
    with cassette_scope("s"):
        await collect(RecordReplayProvider("record", tmp_path, FakeProvider(["a"])), req("original prompt"))
    with cassette_scope("s"), pytest.raises(CassetteMismatch) as ei:
        await collect(RecordReplayProvider("replay", tmp_path), req("edited prompt"))
    assert "original prompt" in str(ei.value) and "edited prompt" in str(ei.value)


async def test_signatures_do_not_affect_the_fingerprint(tmp_path: Path) -> None:
    """thoughtSignatures differ between recordings; they mustn't break replay matching."""
    from app.agents.providers.base import ToolCall

    def with_sig(sig: str) -> ChatRequest:
        call = ToolCall("call_0_0", "t", {"q": 1}, {"model": "m", "thought_signature": sig})
        return ChatRequest(system="s", messages=[Message("user", "q"), Message("assistant", "", tool_calls=[call]), Message("tool", "{}", tool_call_id="call_0_0", name="t")])

    with cassette_scope("s"):
        await collect(RecordReplayProvider("record", tmp_path, FakeProvider(["a"])), with_sig("one"))
    with cassette_scope("s"):
        events = await collect(RecordReplayProvider("replay", tmp_path), with_sig("two"))
    assert events[0] == TextDelta("a")


async def test_requires_a_scope(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        await collect(RecordReplayProvider("replay", tmp_path), req())


async def test_scopes_are_independent(tmp_path: Path) -> None:
    rec = RecordReplayProvider("record", tmp_path, FakeProvider(["a", "b"]))
    for name in (f"x-{uuid.uuid4().hex[:4]}", f"y-{uuid.uuid4().hex[:4]}"):
        with cassette_scope(name):
            await collect(rec, req())
            assert json.loads((tmp_path / f"{name}.json").read_text())["calls"][0]["index"] == 0


class _FailsOnce(FakeProvider):
    """Fails its second call (like a mid-turn HTTP 503), then answers normally."""

    def __init__(self, script: list) -> None:
        super().__init__(script)
        self.calls = 0

    async def stream(self, r: ChatRequest):  # noqa: ANN201
        self.calls += 1
        if self.calls == 2:
            from app.agents.providers.base import ProviderError

            raise ProviderError("gemini HTTP 503: high demand")
        async for e in super().stream(r):
            yield e


async def test_a_failed_call_never_shifts_later_calls(tmp_path: Path) -> None:
    """Seen in a live eval: a call that raised was not saved but used up its index, so the
    next call was stored in its slot and replay compared it against the wrong request."""
    rec = RecordReplayProvider("record", tmp_path, _FailsOnce(["a", "c"]))
    with cassette_scope("s"):
        await collect(rec, req("one"))
        with pytest.raises(Exception, match="503"):
            await collect(rec, req("two"))
        third = await collect(rec, req("three"))
    calls = json.loads((tmp_path / "s.json").read_text())["calls"]
    assert [c["index"] for c in calls] == [0, 2]

    replay = RecordReplayProvider("replay", tmp_path)
    with cassette_scope("s"):
        await collect(replay, req("one"))
        with pytest.raises(CassetteMissing, match="call 1"):  # the failed call: clearly missing
            await collect(replay, req("two"))
        assert await collect(replay, req("three")) == third  # not shifted onto call 1's slot
