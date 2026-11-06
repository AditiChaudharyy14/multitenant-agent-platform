"""Recorded fixtures and cassettes must never contain an API key."""

import re
from pathlib import Path

ROOTS = [Path(__file__).resolve().parents[1] / "fixtures", Path("/eval/cassettes")]
KEY_PATTERNS = [re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), re.compile(r"x-goog-api-key", re.I), re.compile(r"Bearer\s+[A-Za-z0-9]{20,}")]


def test_no_api_keys_in_recordings() -> None:
    files = [p for root in ROOTS if root.exists() for p in root.rglob("*.json")]
    assert files, "expected recorded fixtures"
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pattern in KEY_PATTERNS:
            assert not pattern.search(text), f"{path} matches {pattern.pattern}"
