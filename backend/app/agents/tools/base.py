"""Tool framework: what a tool is, and what it hands back to the loop.

A tool's argument schema (args_model) is all the model ever sees. Identity never appears
in it: the handler gets the server-built ToolContext as its first argument instead.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.agents.providers.base import ToolSpec
from app.tenancy.context import ToolContext


@dataclass(frozen=True, slots=True)
class Citation:
    id: str  # doc_id#section
    title: str  # the document title, for the UI


@dataclass(frozen=True, slots=True)
class ToolResult:
    content_for_model: str  # what goes back to the LLM as the tool message
    cards: list[dict[str, Any]] = field(default_factory=list)  # UI cards (proposals, sources…)
    citations: list[Citation] = field(default_factory=list)  # sources the model may cite this turn
    activity_label: str | None = None  # replaces the tool's own label on tool_end, if set
    ok: bool = True  # False: the call failed (bad args, unknown tool, handler error)


Handler = Callable[[ToolContext, Any], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str  # written for the model: when to call it and how to fill the args
    args_model: type[BaseModel]  # validated before the handler runs; use extra="forbid"
    handler: Handler
    activity_label: str  # UI text while it runs, e.g. "Checking stock…"

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.args_model.model_json_schema())


def error_result(message: str) -> ToolResult:
    """A failed call, reported to the model as data so it can recover (not a crash)."""
    return ToolResult(f"ERROR: {message}", ok=False)
