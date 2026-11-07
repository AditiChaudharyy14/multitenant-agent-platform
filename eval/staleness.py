"""Stale cassettes: say so plainly, and never let them trigger live recording.

A cassette is stale when the request the agent sends today (system prompt, tools, history)
no longer matches the one it was recorded with, e.g. after a tool or a prompt rule was
added. Replaying it can only fail, and re-recording is a deliberate, quota-spending step.

Pure functions (no database, no app imports) so tests/eval can check them offline.
"""

from __future__ import annotations

STALE_PREFIX = "CassetteMismatch"

BANNER = """
==============================================================================
 {n} cassette(s) are STALE: the agent's requests changed since they were
 recorded (a new tool or prompt rule changes every fingerprint).
 This is expected after such a change, not a regression.
   {keys}{missing}
 Nothing was recorded live, and eval/results.md was left as it was.
 Re-record once the daily quota allows (needs ~3 requests per turn):
   make eval-live            # the whole suite
   make eval-live ONLY=...   # or just these
==============================================================================
"""


def is_stale(run_error: str | None) -> bool:
    return bool(run_error) and run_error.startswith(STALE_PREFIX)


def order_for_run(modes: dict[str, str]) -> list[str]:
    """Scenario keys with every replay before every record, so a stale replay is seen
    before any live recording starts (stable within each group)."""
    return [k for k, m in modes.items() if m != "record"] + [k for k, m in modes.items() if m == "record"]


def may_record(stale: list[str]) -> bool:
    """Auto mode records missing cassettes only while no replay has turned out stale: a
    stale suite is re-recorded as a whole, on purpose, with make eval-live."""
    return not stale


def _short(keys: list[str]) -> str:
    return ", ".join(keys[:12]) + (f", … (+{len(keys) - 12} more)" if len(keys) > 12 else "")


def banner(stale: list[str], missing: list[str] | tuple[str, ...] = ()) -> str:
    """`missing`: scenarios that were never recorded (new ones), recorded in the same batch."""
    extra = f"\n Not recorded yet ({len(missing)}): {_short(list(missing))}" if missing else ""
    return BANNER.format(n=len(stale), keys=_short(stale), missing=extra)
