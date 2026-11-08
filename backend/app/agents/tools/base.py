"""Tool framework: what a tool is, and what it hands back to the loop.

A tool's argument schema (args_model) is all the model ever sees. Identity never appears
in it: the handler gets the server-built ToolContext as its first argument instead.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.agents.providers.base import ToolSpec
from app.domain.reply import Reply
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

# Appended to every live-data tool's description (Tool.live).
FRESHNESS = (
    "Call this again each time you state these facts: results from earlier in the conversation " "may be out of date."
)


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str  # written for the model: when to call it and how to fill the args
    args_model: type[BaseModel]  # validated before the handler runs; use extra="forbid"
    handler: Handler
    activity_label: str  # UI text while it runs, e.g. "Checking stock…"
    # Returns live facts (prices, stock, status, availability): the model is told to call it
    # again whenever it states them, instead of repeating an earlier result.
    live: bool = False

    def spec(self) -> ToolSpec:
        description = f"{self.description} {FRESHNESS}" if self.live else self.description
        return ToolSpec(self.name, description, plain_schema(self.args_model.model_json_schema()))


def error_result(message: str) -> ToolResult:
    """A failed call, reported to the model as data so it can recover (not a crash)."""
    return ToolResult(f"ERROR: {message}", ok=False)


def from_reply(reply: Reply, activity_label: str | None = None) -> ToolResult:
    """A domain Reply as a tool result (refusals are answers, not failures: ok stays True)."""
    return ToolResult(reply.text, cards=list(reply.cards), activity_label=activity_label, ok=reply.ok)


def plain_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The JSON schema in the subset every provider accepts.

    Pydantic emits $defs/$ref for nested models and anyOf[X, null] for optional fields;
    Gemini's function schema rejects $ref. Refs are inlined, an optional field becomes
    plain X (it is simply not required), and a null default is dropped.
    """
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:  # "#/$defs/Name"
            return walk({**defs[node["$ref"].rsplit("/", 1)[-1]], **{k: v for k, v in node.items() if k != "$ref"}})
        out = {k: walk(v) for k, v in node.items() if k != "$defs"}
        options = out.get("anyOf")
        if isinstance(options, list):
            non_null = [o for o in options if o.get("type") != "null"]
            if len(non_null) == 1 and len(options) == 2:
                out.pop("anyOf")
                out = {**non_null[0], **out}
        if out.get("default", 0) is None:
            out.pop("default")
        return out

    return walk(schema)
