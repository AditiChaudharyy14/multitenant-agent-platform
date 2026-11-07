"""Provider-neutral types for talking to an LLM.

The agent loop only ever sees these. Each adapter translates them to and from one vendor's
wire format, so swapping Gemini for Mistral (or a FakeProvider in tests) is config only.
"""

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments the model may pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Opaque, vendor-specific state that must travel back with this call on later requests
    # (Gemini 3's thoughtSignature, plus the model that produced it). Never shown to users.
    provider_meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant turns only
    tool_call_id: str | None = None  # tool turns: which call this answers
    name: str | None = None  # tool turns: the tool's name


@dataclass(frozen=True)
class ChatRequest:
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    temperature: float = 0.2
    max_output_tokens: int = 1024


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallEvent:
    call: ToolCall


@dataclass(frozen=True)
class Finished:
    finish_reason: str  # stop | tool_calls | length | blocked | other
    usage: Usage
    model: str  # the model that actually answered (after any fallback)


Event = TextDelta | ToolCallEvent | Finished


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def stream(self, req: ChatRequest) -> AsyncIterator[Event]:
        """Stream one model response: text deltas and/or tool calls, then exactly one Finished."""

    def pin(self, model: str) -> "LLMProvider":
        """This provider restricted to `model`, for the rest of a turn.

        The agent loop calls it after a turn's first response, so every later step of the
        turn goes to the model that answered first and never falls back mid-turn. A
        provider with a single model has nothing to restrict and returns itself.
        """
        return self


# ───────────── errors ─────────────


class LLMError(Exception):
    """Base class for everything the provider layer raises."""


class ProviderAuthError(LLMError):
    """Missing or rejected API key."""


class ProviderError(LLMError):
    """A non-retryable error response (bad request, server error)."""


class ProviderUnavailable(LLMError):
    """Every model was tried and none answered (rate limited), but budget remains."""


class RateLimited(LLMError):
    def __init__(self, message: str, retry_after: float | None, daily: bool) -> None:
        super().__init__(message)
        self.retry_after = retry_after  # seconds the vendor asked us to wait, if it said
        self.daily = daily  # True: the per-day quota is spent, retrying today is pointless


class ProviderOverloaded(RateLimited):
    """A transient server-side failure (HTTP 500/502/503/504, e.g. Gemini's "high demand").
    Handled exactly like a transient 429: back off, retry, then fall back to the next model."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message, retry_after, daily=False)


TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})


class DailyBudgetExhausted(LLMError):
    """Our own per-model daily budget is spent for every configured model. Fails fast."""

    def __init__(self, provider: str, usage: list[tuple[str, int, int]], resets_at: datetime) -> None:
        from zoneinfo import ZoneInfo

        self.provider, self.usage, self.resets_at = provider, usage, resets_at
        models = ", ".join(f"{m} {used}/{limit}" for m, used, limit in usage)
        pt = resets_at.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M %Z")
        npt = resets_at.astimezone(ZoneInfo("Asia/Kathmandu")).strftime("%H:%M")
        super().__init__(
            f"LLM daily budget exhausted for {provider}: {models}. Resets at {pt} PT "
            f"({npt} NPT). Wait for the reset, raise LLM_DAILY_BUDGET_{provider.upper()} if "
            f"your quota allows, or run evals with EVAL_MODE=replay."
        )


# ───────────── serialisation (cassettes) ─────────────


def request_to_dict(req: ChatRequest) -> dict[str, Any]:
    """The request as plain JSON, minus provider_meta: thoughtSignatures differ between
    recordings and must not change the fingerprint."""
    d = asdict(req)
    for m in d["messages"]:
        for c in m["tool_calls"]:
            c.pop("provider_meta", None)
    return d


def request_fingerprint(req: ChatRequest) -> str:
    canonical = json.dumps(request_to_dict(req), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def event_to_dict(e: Event) -> dict[str, Any]:
    return {"type": type(e).__name__, **asdict(e)}


def event_from_dict(d: dict[str, Any]) -> Event:
    d = dict(d)
    kind = d.pop("type")
    if kind == "TextDelta":
        return TextDelta(**d)
    if kind == "ToolCallEvent":
        return ToolCallEvent(ToolCall(**d["call"]))
    if kind == "Finished":
        return Finished(d["finish_reason"], Usage(**d["usage"]), d["model"])
    raise ValueError(f"unknown event type {kind!r}")
