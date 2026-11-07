"""Record real LLM responses once, replay them for free (EVAL_MODE=record|replay).

The eval runner wraps each scenario in `with cassette_scope("store/cancel_pending"):`.
Inside it, the n-th model call is stored as call n of eval/cassettes/store/cancel_pending.json,
holding a fingerprint of the normalised request and the normalised events (not raw HTTP,
so a cassette works whichever provider recorded it).

- record: calls the real provider (which counts against the daily budget) and saves the
  events. Recording call 0 of a scenario starts its cassette afresh.
- replay: no network, no budget, no API key. A missing call raises CassetteMissing; a
  request that differs from the recording raises CassetteMismatch, so an edited prompt can
  never silently replay stale answers. Re-record just that scenario.

Requirement for later milestones: anything the server generates at runtime and feeds back
to the model (proposal ids, timestamps) must be deterministic under evals (SEED_NOW,
frozen clock, deterministic ids), or every replay after the first tool call will mismatch.
"""

import difflib
import json
import os
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.agents.providers.base import (
    ChatRequest,
    Event,
    LLMError,
    LLMProvider,
    event_from_dict,
    event_to_dict,
    request_fingerprint,
    request_to_dict,
)


# Cassettes are committed to git, so nothing secret may ever be written into one. They hold
# normalised requests and events only (never raw HTTP), so this is defence in depth: drop
# any "headers" mapping and redact anything that looks like an API key or key parameter.
_SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),  # Google API key
    re.compile(r"(?i)(x-goog-api-key\s*[:=]\s*)[^\s\"',&]+"),
    re.compile(r"(?i)([?&]key=)[^\s\"',&]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
)
REDACTED = "[REDACTED]"


def _secret_values() -> list[str]:
    """The configured API keys themselves, so they are removed even in an unknown format."""
    values = [os.environ.get(k, "") for k in ("GEMINI_API_KEY", "MISTRAL_API_KEY")]
    return [v for v in values if len(v) >= 8]


def scrub(value: object) -> object:
    """A copy of `value` (JSON-like) with headers dropped and secrets redacted."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k.lower() != "headers"}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        for secret in _secret_values():
            value = value.replace(secret, REDACTED)
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + REDACTED, value)
    return value


class CassetteMissing(LLMError):
    pass


class CassetteMismatch(LLMError):
    pass


@dataclass
class _Scope:
    scenario: str
    index: int = 0  # next call number within the scenario


_scope: ContextVar[_Scope | None] = ContextVar("cassette_scope", default=None)


@contextmanager
def cassette_scope(scenario: str) -> Iterator[None]:
    """Name the scenario whose calls are being recorded or replayed (e.g. 'store/reorder')."""
    token = _scope.set(_Scope(scenario))
    try:
        yield
    finally:
        _scope.reset(token)


class RecordReplayProvider(LLMProvider):
    def __init__(self, mode: Literal["record", "replay"], cassette_dir: str | Path, inner: LLMProvider | None = None) -> None:
        if mode == "record" and inner is None:
            raise ValueError("record mode needs the real provider to record from")
        self.mode, self.dir, self.inner = mode, Path(cassette_dir), inner
        self.name = inner.name if inner else "replay"

    def pin(self, model: str) -> "RecordReplayProvider":
        # Replay has no live model to restrict. Record pins the provider it records from.
        if self.inner is None:
            return self
        return RecordReplayProvider(self.mode, self.dir, self.inner.pin(model))  # type: ignore[arg-type]

    def _path(self, scenario: str) -> Path:
        return self.dir / f"{scenario}.json"

    async def stream(self, req: ChatRequest) -> AsyncIterator[Event]:
        scope = _scope.get()
        if scope is None:
            raise RuntimeError("EVAL_MODE is record/replay but no cassette_scope() is active")
        index = scope.index
        scope.index += 1
        if self.mode == "replay":
            for event in self._replay(scope.scenario, index, req):
                yield event
            return
        assert self.inner is not None
        events = []
        async for event in self.inner.stream(req):
            events.append(event)
            yield event
        self._save(scope.scenario, index, req, events)

    def _replay(self, scenario: str, index: int, req: ChatRequest) -> list[Event]:
        hint = f"re-record just this scenario: make eval-live ONLY={scenario} (EVAL_MODE=record)"
        path = self._path(scenario)
        calls = json.loads(path.read_text(encoding="utf-8"))["calls"] if path.exists() else []
        if index >= len(calls):
            raise CassetteMissing(f"no recording for {scenario} call {index}; {hint}")
        call = calls[index]
        if call["fingerprint"] != request_fingerprint(req):
            recorded = json.dumps(call["request"], indent=1, sort_keys=True, ensure_ascii=False).splitlines()
            current = json.dumps(request_to_dict(req), indent=1, sort_keys=True, ensure_ascii=False).splitlines()
            diff = "\n".join(list(difflib.unified_diff(recorded, current, "recorded", "current", lineterm="", n=1))[:40])
            raise CassetteMismatch(f"{scenario} call {index}: the request changed since it was recorded; {hint}\n{diff}")
        return [event_from_dict(e) for e in call["events"]]

    def _save(self, scenario: str, index: int, req: ChatRequest, events: list[Event]) -> None:
        path = self._path(scenario)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Call 0 starts the scenario afresh, so a re-recording never keeps stale later calls.
        calls = [] if index == 0 or not path.exists() else json.loads(path.read_text(encoding="utf-8"))["calls"][:index]
        calls.append({
            "index": index,
            "fingerprint": request_fingerprint(req),
            "request": request_to_dict(req),
            "events": [event_to_dict(e) for e in events],
        })  # fmt: skip
        tmp = path.with_suffix(".json.tmp")
        # The fingerprint is computed from the real request above; only the stored copy is scrubbed.
        cassette = scrub({"scenario": scenario, "calls": calls})
        tmp.write_text(json.dumps(cassette, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)  # atomic: an interrupted recording never leaves half a file
