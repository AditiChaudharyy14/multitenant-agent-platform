"""The quota floor for live eval recording. Pure functions, so tests/eval checks them offline.

The floor counts what is left across *every* model of the provider (primary + fallbacks),
because the router falls back to the next model when the primary's daily budget runs out,
and the LLM-as-judge spends the fallback model directly. A scenario is recorded only if,
after its worst-case cost, at least `floor + reserve` requests would still be left:
`floor` is kept back for the live demo, `reserve` for later steps of the same session
(e.g. the judge after a pass^k run).
"""

from __future__ import annotations

# The most requests one scenario has taken so far is about 10 (a 4-turn conversation with
# several tool steps per turn, plus a 503 retry). 15 leaves headroom.
WORST_CASE_PER_SCENARIO = 15
DEFAULT_FLOOR = 120


def may_record(left_combined: int, floor: int = DEFAULT_FLOOR, reserve: int = 0,
               worst: int = WORST_CASE_PER_SCENARIO) -> bool:  # fmt: skip
    """True if one more scenario can be recorded without going below floor + reserve."""
    return left_combined - worst >= floor + reserve


def left_combined(used: dict[str, int], limit: int) -> int:
    """Requests left today across all models, each with the same daily limit."""
    return sum(max(limit - n, 0) for n in used.values())
