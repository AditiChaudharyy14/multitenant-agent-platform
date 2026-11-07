"""Committed eval cassettes never contain secrets, and the recorder scrubs them.

Cassettes are in git so reviewers can run `make eval` offline. They hold normalised
requests and events only, never raw HTTP; these tests make sure it stays that way.
"""

import json
import os
import re
from pathlib import Path

import pytest

from app.agents.providers.recording import REDACTED, scrub

CASSETTES = sorted(Path("/eval/cassettes").rglob("*.json"))
FORBIDDEN = [
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),  # Google API key
    re.compile(r"x-goog-api-key", re.I),
    re.compile(r"[?&]key=", re.I),
    re.compile(r"\bauthorization\b", re.I),
    re.compile(r"\bbearer\s+[A-Za-z0-9._\-]{8,}", re.I),
]


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return {k.lower() for k in node} | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


@pytest.mark.parametrize("path", CASSETTES, ids=lambda p: str(p.relative_to("/eval/cassettes")))
def test_cassette_holds_no_secret_or_header(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    for pattern in FORBIDDEN:
        assert not pattern.search(raw), f"{path.name} matches {pattern.pattern}"
    for var in ("GEMINI_API_KEY", "MISTRAL_API_KEY"):
        value = os.environ.get(var, "")
        if len(value) >= 8:
            assert value not in raw, f"{path.name} contains {var}"
    keys = _keys(json.loads(raw))
    assert "headers" not in keys and "x-goog-api-key" not in keys


def test_scrub_drops_headers_and_redacts_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value-123456")
    dirty = {
        "headers": {"x-goog-api-key": "abc"},
        "calls": [{"text": "see https://x.test/v1?alt=sse&key=abcdef and AIza" + "A" * 35},
                  {"text": "x-goog-api-key: zzzzzz, Bearer abcdefghijkl, secret-value-123456"}],
    }  # fmt: skip
    clean = json.dumps(scrub(dirty))
    assert "headers" not in clean
    for leaked in ("abcdef", "AIza", "zzzzzz", "abcdefghijkl", "secret-value-123456"):
        assert leaked not in clean
    assert REDACTED in clean


URL_WITH_PASSWORD = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@'\"<>]+:(?!\*\*\*@)[^\s'\"<>@]+@", re.I)


@pytest.mark.parametrize("path", [p for p in (Path("/eval/results.json"), Path("/eval/results.md")) if p.exists()],
                         ids=lambda p: p.name)  # fmt: skip
def test_committed_results_hold_no_credentials(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    assert not URL_WITH_PASSWORD.search(raw), f"{path.name} contains a URL with a password"
    for pattern in FORBIDDEN:
        assert not pattern.search(raw), f"{path.name} matches {pattern.pattern}"
