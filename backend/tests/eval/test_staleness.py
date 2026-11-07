"""make eval reports stale cassettes clearly and never records live because of them."""

import sys
from pathlib import Path

import pytest

EVAL_DIR = Path("/eval")
if not (EVAL_DIR / "staleness.py").exists():
    pytest.skip("eval/ is not mounted here", allow_module_level=True)
sys.path.insert(0, str(EVAL_DIR))

from staleness import banner, is_stale, may_record, order_for_run  # noqa: E402


def test_a_fingerprint_mismatch_is_stale_but_a_missing_cassette_is_not() -> None:
    assert is_stale("CassetteMismatch: admin/store_today call 0: the request changed")
    assert not is_stale("CassetteMissing: no recording for store/x call 0")
    assert not is_stale(None) and not is_stale("")


def test_replays_run_before_any_recording() -> None:
    modes = {"a/new": "record", "b/old": "replay", "c/new": "record", "d/old": "replay"}
    assert order_for_run(modes) == ["b/old", "d/old", "a/new", "c/new"]


def test_no_live_recording_once_anything_is_stale() -> None:
    assert may_record([])
    assert not may_record(["admin/store_today"])


def test_banner_names_the_fix_and_the_scenarios() -> None:
    text = banner([f"store/s{i}" for i in range(15)])
    assert "15 cassette(s) are STALE" in text and "make eval-live" in text
    assert "store/s0" in text and "(+3 more)" in text
    assert "Not recorded yet" not in text
    with_new = banner(["admin/a"], ["store/memory_new"])
    assert "Not recorded yet (1): store/memory_new" in with_new
