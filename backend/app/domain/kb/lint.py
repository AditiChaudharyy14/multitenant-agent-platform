"""Seed-time lint: KB markdown must contain no prices or stock figures (invariant 8).

Prices and stock are live facts that change in the database; a copy in the KB would go
stale and the agent might quote it. `make seed` fails on any finding, before it writes.
"""

import re
from pathlib import Path

# Currency markers anywhere on a line. Bare words, not just "NPR <digits>", because
# "450 NPR" or "Rs.450" are just as stale.
_CURRENCY = re.compile(r"\bNPR\b|\bRs\b\.?|रु|₹|\brupees?\b|\$\s*\d", re.IGNORECASE)
# Stock figures: a number next to a stock word. "out of stock" on its own is fine; it's
# a policy statement, not a figure.
_STOCK = re.compile(
    r"\b\d+\s*(?:units?\s+|pcs\.?\s+|pieces\s+|packs?\s+)?"
    r"(?:in\s+stock|left|available|remaining)\b"
    r"|\bstock\s*(?:level|count)?\s*(?:of|:|=)?\s*\d+",
    re.IGNORECASE,
)


class KBLintError(ValueError):
    """One or more KB files contain live facts; the message lists every file:line."""


def lint_text(text: str) -> list[str]:
    """Findings as 'line N: <kind> <match>'; an empty list means clean."""
    findings = []
    for n, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in (("currency", _CURRENCY), ("stock figure", _STOCK)):
            for m in pattern.finditer(line):
                findings.append(f"line {n}: {kind} {m.group(0)!r}")
    return findings


def lint_kb_dir(kb_dir: str | Path) -> None:
    """Lint every *.md under kb_dir; raise KBLintError listing all findings."""
    problems = []
    for path in sorted(Path(kb_dir).glob("*.md")):
        for finding in lint_text(path.read_text(encoding="utf-8")):
            # "line N: ..." → "<file>:N: ..." so editors can jump to it.
            problems.append(f"{path}:{finding.removeprefix('line ')}")
    if problems:
        raise KBLintError("KB contains prices or stock figures:\n  " + "\n  ".join(problems))
