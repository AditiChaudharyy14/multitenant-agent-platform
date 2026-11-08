"""Rebuild the model's view of earlier turns from stored messages.

An assistant row stores its tool-calling steps in tool_calls; each step becomes the
assistant's tool request plus one tool message per call, so the model sees what it looked
up before. Old tool results are truncated: they are context now, not the answer.
"""

from collections.abc import Iterable
from typing import Any

from app.agents.providers.base import Message, ToolCall

HISTORY_LIMIT = 20  # stored messages (user + assistant rows) sent as history
# ... and at most this many tokens of it (estimated at ~4 characters per token), oldest
# dropped first, so a few very long turns can't crowd out the prompt or the answer.
HISTORY_TOKEN_BUDGET = 6000
CHARS_PER_TOKEN = 4
TOOL_RESULT_LIMIT = 1500  # characters kept from each earlier tool result
_MARK = " …[truncated]"


def truncate(text: str, limit: int = TOOL_RESULT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - len(_MARK)] + _MARK


def to_messages(rows: Iterable[Any]) -> list[Message]:
    out: list[Message] = []
    for r in rows:
        if r.role == "user":
            out.append(Message("user", r.content))
            continue
        for step in r.tool_calls or []:
            calls = [ToolCall(c["id"], c["name"], c["arguments"], c.get("provider_meta")) for c in step["calls"]]
            if not calls:
                continue
            out.append(Message("assistant", "", tool_calls=calls))
            out += [Message("tool", truncate(c["result"]), tool_call_id=c["id"], name=c["name"]) for c in step["calls"]]
        if r.content:
            out.append(Message("assistant", r.content))
    # Over the token budget: drop the oldest messages (keeping at least the newest one).
    while len(out) > 1 and estimated_tokens(out) > HISTORY_TOKEN_BUDGET:
        out.pop(0)
    # The window may now open mid-turn; the model must see a user message first.
    while out and out[0].role != "user":
        out.pop(0)
    return out


def estimated_tokens(messages: list[Message]) -> int:
    """A cheap, provider-neutral estimate: characters / 4, tool-call arguments included."""
    chars = sum(len(m.content) + sum(len(str(c.arguments)) for c in m.tool_calls) for m in messages)
    return chars // CHARS_PER_TOKEN
