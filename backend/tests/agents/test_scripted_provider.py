"""ScriptedProvider: the YAML-scripted offline model the Playwright tests run against.

It is stateless (each request is answered from the request alone), so these tests drive it
through the real agent loop, with the loop's own message bookkeeping deciding the step.
"""

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.providers import get_provider
from app.agents.providers.base import ChatRequest, Message, TextDelta
from app.agents.providers.scripted import Scenario, ScriptedProvider, load_scenario
from app.config import get_settings
from tests.agents.test_loop import kinds, run

E2E_SCENARIO = Path(__file__).resolve().parents[2] / "fake_scenarios" / "e2e.yaml"

SCENARIO = Scenario.model_validate({
    "default": "I can only answer scripted questions.",
    "rules": [
        {"match": "return", "steps": [{"tool": "search_knowledge", "args": {"query": "returns"}},
                                      {"text": "Unopened items within 14 days {source}."}]},
        {"match": "slow", "steps": [{"text": "one two three four five six", "chunk_delay_ms": 200}]},
    ],
})  # fmt: skip


async def test_rule_calls_the_tool_then_answers_citing_what_was_retrieved() -> None:
    events, state = await run(ScriptedProvider(SCENARIO), user="What is your RETURN policy?")
    assert kinds(events) == ["tool_start", "card", "tool_end", "token", "citation"]  # the stand-in KB tool adds a card
    assert state.tools_called == ["search_knowledge"]
    assert state.content == "Unopened items within 14 days [faq#returns]."
    assert [c.id for c in state.citations] == ["faq#returns"]  # survived the server-side filter


async def test_unmatched_message_gets_the_default_answer() -> None:
    events, state = await run(ScriptedProvider(SCENARIO), user="hello")
    assert kinds(events) == ["token"]
    assert state.content == SCENARIO.default and state.tools_called == []


async def test_step_comes_from_this_turn_only() -> None:
    """A tool call in an earlier turn doesn't advance the script for the new message."""
    from app.agents.providers.base import ToolCall

    history = [Message("user", "returns?"), Message("assistant", tool_calls=[ToolCall("x", "search_knowledge", {})]),
               Message("tool", "<kb_source id=\"faq#old\">…</kb_source>", tool_call_id="x"),
               Message("assistant", "Old answer."), Message("user", "and returns again?")]  # fmt: skip
    events = [e async for e in ScriptedProvider(SCENARIO).stream(ChatRequest("sys", history))]
    assert events[0].call.name == "search_knowledge"  # step 0 again, not step 1


async def test_text_streams_in_small_pieces_with_a_cancellable_delay() -> None:
    provider = ScriptedProvider(SCENARIO)
    pieces: list[str] = []

    async def consume() -> None:
        async for ev in provider.stream(ChatRequest("sys", [Message("user", "slow please")])):
            if isinstance(ev, TextDelta):
                pieces.append(ev.text)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # the first piece is immediate, the next waits 200 ms
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pieces == ["one two "]  # Stop mid-stream leaves a partial answer


def test_bad_scenarios_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate({"default": "x", "rules": [{"match": "a", "steps": [{"tool": "t", "text": "both"}]}]})
    with pytest.raises(ValidationError):
        Scenario.model_validate({"default": "x", "rules": [{"match": "(", "steps": [{"text": "bad regex"}]}]})


def test_the_e2e_scenario_file_is_valid_and_selected_by_settings() -> None:
    scenario = load_scenario(E2E_SCENARIO)
    assert any(s.tool == "search_knowledge" for r in scenario.rules for s in r.steps)
    s = get_settings().model_copy(update={"llm_provider": "fake", "fake_scenario_file": str(E2E_SCENARIO)})
    assert isinstance(get_provider(s), ScriptedProvider)


async def test_capture_groups_from_the_user_message_fill_tool_args() -> None:
    """`{1}` in a tool arg is the rule regex's first group, so a script can say 'track order HT-1042'."""
    scenario = Scenario.model_validate({
        "default": "-",
        "rules": [{"match": r"status of order (HT-\d+)", "steps": [{"tool": "get_order_status", "args": {"order_number": "{1}"}}]}],
    })  # fmt: skip
    history = [Message("user", "What's the status of order HT-1042?")]
    events = [e async for e in ScriptedProvider(scenario).stream(ChatRequest("sys", history))]
    assert events[0].call.arguments == {"order_number": "HT-1042"}
