"""FakeProvider: a scripted, offline LLM for tests. `make test` never uses anything else.

    fake = FakeProvider([tool_call("search_knowledge", {"query": "returns"}), "Here's the policy."])

Each stream() call consumes one turn: a string streams as text, a ToolCall (or a list of
them) is emitted as tool calls, an exception is raised, and a callable receives the request
and returns one of those. Every request is kept in `fake.requests` for assertions.
"""

from collections.abc import AsyncIterator, Callable
from typing import Any, cast

from app.agents.providers.base import (
    ChatRequest,
    Event,
    Finished,
    LLMProvider,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    Usage,
)

Turn = str | ToolCall | list[ToolCall] | Exception | Callable[[ChatRequest], Any]


def tool_call(name: str, arguments: dict[str, Any], id: str | None = None) -> ToolCall:  # noqa: A002
    return ToolCall(id or f"fake_{name}", name, arguments)


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, script: list[Turn] | None = None, model: str = "fake-model") -> None:
        self.script = list(script or [])
        self.model = model
        self.requests: list[ChatRequest] = []

    async def stream(self, req: ChatRequest) -> AsyncIterator[Event]:
        self.requests.append(req)
        if not self.script:
            raise AssertionError("FakeProvider script exhausted: the test scripted fewer turns than were requested")
        turn = self.script.pop(0)
        if callable(turn) and not isinstance(turn, ToolCall):
            turn = turn(req)
        if isinstance(turn, Exception):
            raise turn
        prompt_tokens = sum(len(m.content.split()) for m in req.messages)
        if isinstance(turn, str):
            words = turn.split(" ")
            # Stream in a few pieces so consumers really handle deltas.
            step = max(1, len(words) // 3)
            for i in range(0, len(words), step):
                piece = " ".join(words[i : i + step])
                yield TextDelta(piece if i + step >= len(words) else piece + " ")
            yield Finished("stop", Usage(prompt_tokens, len(words)), self.model)
            return
        calls: list[ToolCall] = turn if isinstance(turn, list) else [cast(ToolCall, turn)]
        for c in calls:
            yield ToolCallEvent(c)
        yield Finished("tool_calls", Usage(prompt_tokens, len(calls)), self.model)
