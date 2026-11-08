"""KB consistency against seed data (run by `make seed` and `make kb-check`).

Two checks, both on what the KB *says* versus what the database will hold:

(a) Opening days and times. For an org with opening_hours in data.yaml, any KB sentence
    saying the business is closed on a weekday, open from one weekday to another, or open
    between HH:MM and HH:MM must match data.yaml exactly. Opening hours are a live fact a
    customer acts on, so a stale KB sentence is a real risk.
(b) canonical_facts in data.yaml: each names a phrase the fact is about and its one true
    value ("unopened" -> "fourteen days"). Every KB sentence that mentions the phrase and
    states a quantity in the same unit must state that value.
"""

import re
from datetime import time
from pathlib import Path

from app.seed.data import SeedData

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_NUM = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fourteen|fifteen|twenty|thirty)"
_UNITS = {
    "days": rf"\b{_NUM}\b(?:\s*(?:to|-|–)\s*{_NUM})?\s+(?:working\s+|business\s+)?days?\b",
    "hours": rf"\b{_NUM}\b(?:\s*(?:to|-|–)\s*{_NUM})?\s+hours?\b",
    "minutes": rf"\b{_NUM}\b(?:\s*(?:to|-|–)\s*{_NUM})?\s+minutes?\b",
    "%": r"\b\d+\s*(?:%|percent)",
}
_DAY = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?"


def _sentences(kb_dir: Path) -> list[tuple[str, str]]:
    """(doc file name, sentence) for every sentence of every KB doc, front matter dropped."""
    out = []
    for path in sorted(kb_dir.glob("*.md")):
        body = re.sub(r"^---\n.*?\n---\n", "", path.read_text(encoding="utf-8"), flags=re.S)
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", body):
            if sentence.strip():
                out.append((path.name, sentence.strip()))
    return out


def _unit(value: str) -> str:
    return "%" if value.strip().endswith(("%", "percent")) else value.split()[-1].rstrip("s") + "s"


def check_facts(kb_dir: Path, data: SeedData) -> list[str]:
    problems = []
    for fact in data.canonical_facts:
        unit = _unit(fact.value)
        for doc, s in _sentences(kb_dir):
            low = s.lower()
            if re.search(fact.mentions, low) and re.search(_UNITS[unit], low) and fact.value.lower() not in low:
                problems.append(f"{doc}: says something about '{fact.mentions}' that isn't {fact.value!r}: {s!r}")
    return problems


def _day_range(first: str, last: str) -> set[int]:
    a, b = DAYS.index(first), DAYS.index(last)
    return {(a + i) % 7 for i in range((b - a) % 7 + 1)}


def check_hours(kb_dir: Path, data: SeedData) -> list[str]:
    if not data.opening_hours:
        return []  # nothing in the database to contradict (e.g. a store without opening hours)
    open_days = {h.weekday for h in data.opening_hours}
    times = {(h.opens, h.closes) for h in data.opening_hours}
    problems = []
    for doc, s in _sentences(kb_dir):
        low = s.lower()
        for m in re.finditer(rf"closed (?:on |every )?{_DAY}", low):
            if DAYS.index(m.group(1)) in open_days:
                problems.append(f"{doc}: says closed on {m.group(1)}, but data.yaml opens then: {s!r}")
        for m in re.finditer(rf"open (?:from )?{_DAY} to {_DAY}", low):
            stated = _day_range(m.group(1), m.group(2))
            if stated != open_days:
                problems.append(f"{doc}: says open {m.group(1)} to {m.group(2)}, data.yaml differs: {s!r}")
        for m in re.finditer(r"open\D{0,30}?(\d{1,2})[:.](\d{2})\s*(?:to|-|–)\s*(\d{1,2})[:.](\d{2})", low):
            span = (time(int(m.group(1)), int(m.group(2))), time(int(m.group(3)), int(m.group(4))))
            if span not in times:
                problems.append(f"{doc}: says open {m.group(0)!r}, not an opening span in data.yaml: {s!r}")
    return problems


def check(kb_dir: Path, data: SeedData) -> list[str]:
    return check_hours(kb_dir, data) + check_facts(kb_dir, data)


def main() -> int:
    from app.config import get_settings
    from app.seed.data import load_seed_data
    from app.tenancy.orgs import load_org_configs

    seed = Path(get_settings().seed_dir)
    found = []
    for cfg in load_org_configs(seed):
        data = load_seed_data(seed / cfg.slug / "data.yaml", cfg.vertical)
        found += [f"{cfg.slug}/kb/{p}" for p in check(seed / cfg.slug / "kb", data)]
    print("\n".join(found) or "KB consistent with data.yaml (opening hours and canonical facts)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
