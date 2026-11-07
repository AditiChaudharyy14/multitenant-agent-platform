"""ScriptedProvider: an offline LLM driven by a YAML scenario file, for UI end-to-end tests.

Selected by LLM_PROVIDER=fake plus FAKE_SCENARIO_FILE. Unlike FakeProvider (a queue that
unit tests fill), it is stateless: every request is answered from the request alone, so
one long-running backend can serve any number of browser sessions in any order.

    rules:
      - match: "deliver"            # regex, case-insensitive, on the last user message
        steps:
          - tool: search_knowledge   # step 0: call a tool
            args: {query: delivery times}
          - text: "Usually 2-4 days {source}."   # step 1: answer; {source} = [first cited id]
            chunk_delay_ms: 40

The step is the number of tool-calling assistant messages since the last user message, so
a rule walks through its steps as the agent loop feeds tool results back. The real tools
run (search_knowledge really searches the org's KB), and the server's citation filter
still checks every [id]; the script only stands in for the model's choices.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.providers.base import (
    ChatRequest,
    Event,
    Finished,
    LLMProvider,
    Message,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    Usage,
)

_SOURCE = re.compile(r'<kb_source id="([^"]+)"')
WORDS_PER_PIECE = 2  # small pieces, so the UI visibly streams


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    text: str | None = None
    chunk_delay_ms: int = Field(default=0, ge=0, le=5000)

    @model_validator(mode="after")
    def _one_kind(self) -> "Step":
        if (self.tool is None) == (self.text is None):
            raise ValueError("a step is either `tool` or `text`")
        return self


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    match: str
    steps: list[Step] = Field(min_length=1)

    @field_validator("match")
    @classmethod
    def _regex(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"bad regex: {exc}") from None
        return v


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default: str
    rules: list[Rule] = Field(default_factory=list)


def load_scenario(path: str | Path) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


class ScriptedProvider(LLMProvider):
    name = "fake"

    def __init__(self, scenario: Scenario, model: str = "scripted-model") -> None:
        self.scenario = scenario
        self.model = model

    async def stream(self, req: ChatRequest) -> AsyncIterator[Event]:
        user, since = _last_user(req.messages)
        step_index = sum(1 for m in since if m.role == "assistant" and m.tool_calls)
        step = self._step(user, step_index)
        prompt_tokens = sum(len(m.content.split()) for m in req.messages)
        if step.tool is not None:
            call_id = f"scripted_{step.tool}_{step_index}"
            yield ToolCallEvent(ToolCall(call_id, step.tool, dict(step.args)))
            yield Finished("tool_calls", Usage(prompt_tokens, 1), self.model)
            return
        text = _fill_sources(step.text or "", since)
        words = text.split(" ")
        for i in range(0, len(words), WORDS_PER_PIECE):
            if step.chunk_delay_ms and i:
                await asyncio.sleep(step.chunk_delay_ms / 1000)  # cancellable: Stop lands here
            piece = " ".join(words[i : i + WORDS_PER_PIECE])
            yield TextDelta(piece if i + WORDS_PER_PIECE >= len(words) else piece + " ")
        yield Finished("stop", Usage(prompt_tokens, len(words)), self.model)

    def _step(self, user: str, index: int) -> Step:
        for rule in self.scenario.rules:
            if found := re.search(rule.match, user, re.IGNORECASE):
                # Past the rule's last step (a tool result it didn't plan for): answer plainly.
                if index >= len(rule.steps):
                    return Step(text=self.scenario.default)
                return _with_groups(rule.steps[index], found)
        return Step(text=self.scenario.default)


def _with_groups(step: Step, found: re.Match[str]) -> Step:
    """Fill `{1}`, `{2}`... in string tool args from the rule regex's groups, so a script can
    act on what the user typed ("status of order HT-1042"). Other braces are left alone."""
    if not step.args or not found.groups():
        return step
    args = dict(step.args)
    for k, v in args.items():
        if isinstance(v, str):
            for n, group in enumerate(found.groups(), start=1):
                v = v.replace(f"{{{n}}}", group or "")
            args[k] = v
    return step.model_copy(update={"args": args})


def _last_user(messages: list[Message]) -> tuple[str, list[Message]]:
    """The last user message's text, and every message after it (this turn so far)."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            return messages[i].content, messages[i + 1 :]
    return "", list(messages)


def _fill_sources(text: str, since: list[Message]) -> str:
    """{source} becomes [id] of the first KB source retrieved this turn (or nothing)."""
    if "{source}" not in text:
        return text
    found = next((m for msg in since if msg.role == "tool" for m in _SOURCE.findall(msg.content)), None)
    return text.replace(" {source}", f" [{found}]" if found else "").replace("{source}", f"[{found}]" if found else "")
