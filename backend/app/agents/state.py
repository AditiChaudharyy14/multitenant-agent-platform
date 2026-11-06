"""What a turn produces, and the events it streams.

TurnState is filled in by the loop as it goes, so whatever exists when a turn stops
(Stop, a disconnect, a provider failure) can still be persisted by the turn service.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from app.agents.citations import filter_citations
from app.agents.providers.base import Usage
from app.agents.tools.base import Citation


@dataclass(frozen=True, slots=True)
class AgentEvent:
    type: str  # token | tool_start | tool_end | card | citation | error | done
    data: dict[str, Any]

    def sse(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"


@dataclass
class TurnState:
    provider: str = ""
    model: str = ""  # the model pinned for this turn (empty until the first response)
    status: str = "running"  # → ok | error | stopped | rate_limited (agent_runs.status)
    error: str | None = None  # internal reason, for agent_runs only; never shown to users
    text_parts: list[str] = field(default_factory=list)  # every streamed token, in order
    steps: list[dict[str, Any]] = field(default_factory=list)  # tool-calling steps, persisted
    cards: list[dict[str, Any]] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    sources: dict[str, Citation] = field(default_factory=dict)  # citable: retrieved this turn
    input_tokens: int = 0
    output_tokens: int = 0
    # Set by finalize():
    content: str = ""  # the text with invented citations removed
    citations: list[Citation] = field(default_factory=list)
    dropped_citations: list[str] = field(default_factory=list)

    def add_usage(self, usage: Usage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens

    def finalize(self) -> None:
        """Keep only citations that search_knowledge returned this turn. Idempotent."""
        self.content, self.citations, self.dropped_citations = filter_citations(
            "".join(self.text_parts), self.sources
        )
