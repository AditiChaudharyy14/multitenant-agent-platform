"""eval/results.json and eval/results.md."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATUS_ICON = {"pass": "✅ pass", "fail": "❌ fail", "error": "⚠️ error", "skipped": "⏭ skipped"}


def explained_ids(failures_md: Path) -> set[str]:
    """Scenario keys (store/xyz) that eval/FAILURES.md has a section for."""
    if not failures_md.exists():
        return set()
    return set(re.findall(r"^##+\s+`?((?:store|booking|admin)/[\w-]+)`?", failures_md.read_text(encoding="utf-8"), re.M))


def merge_previous(path: Path, fresh: list[dict]) -> list[dict]:
    """A partial run (--only) replaces just its scenarios in the previous full results."""
    if not path.exists():
        return fresh
    previous = json.loads(path.read_text(encoding="utf-8")).get("scenarios", [])
    by_key = {r["key"]: r for r in previous}
    by_key.update({r["key"]: r for r in fresh})
    order = [r["key"] for r in previous] + [r["key"] for r in fresh if r["key"] not in {p["key"] for p in previous}]
    return [by_key[k] for k in order]


def _models(r: dict) -> list[str]:
    return sorted({t["model"] for t in r.get("turns", []) if t.get("model") and t["model"] != "unknown"})


def _model_label(r: dict, primary: str) -> str:
    models = _models(r)
    if not models:
        return "—"
    return ", ".join(m if m == primary else f"{m} (fallback)" for m in models)


def _tokens(r: dict) -> tuple[int, int]:
    return (sum(t.get("input_tokens", 0) for t in r.get("turns", [])), sum(t.get("output_tokens", 0) for t in r.get("turns", [])))


def _latency(r: dict) -> int:
    return sum(t.get("latency_ms", 0) for t in r.get("turns", []))


def write(out: Path, results: list[dict], primary: str, models: list[str], elapsed: float, explained: set[str]) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    (out / "results.json").write_text(json.dumps(
        {"generated_at": now, "primary_model": primary, "models": models, "elapsed_s": round(elapsed, 1),
         "scenarios": results}, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")  # fmt: skip

    lines = ["# Eval results", "",
             f"Generated {now}. Models: primary `{primary}`, fallback {', '.join(f'`{m}`' for m in models[1:]) or 'none'}. "
             f"Wall time {elapsed:.1f}s.", "",
             "Each scenario runs on its own clone of the seeded template database with the clock frozen, and is "
             "checked against **database state** as well as the reply. `mode` is `replay` (recorded cassette, no "
             "network) or `record` (live). Latency and tokens are those of the recording.", "",
             "## Summary", "",
             "| Agent | Passed | Failed | Errors | Skipped | Explained in FAILURES.md | Answered by fallback | Tokens in / out |",
             "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for vertical in sorted({r["vertical"] for r in results}):
        rs = [r for r in results if r["vertical"] == vertical]
        count = lambda st: sum(r["status"] == st for r in rs)  # noqa: E731
        fallback = sum(any(m != primary for m in _models(r)) for r in rs)
        tin, tout = map(sum, zip(*[_tokens(r) for r in rs], strict=True)) if rs else (0, 0)
        expl = sum(r["status"] != "pass" and r["key"] in explained for r in rs)
        lines.append(f"| {vertical} | {count('pass')}/{len(rs)} | {count('fail')} | {count('error')} | {count('skipped')} | "
                     f"{expl} | {fallback} | {tin:,} / {tout:,} |")  # fmt: skip

    for vertical in sorted({r["vertical"] for r in results}):
        lines += ["", f"## {vertical}", "",
                  "| Scenario | Result | Mode | Model | Tools called | Tokens in/out | Latency |", "|---|---|---|---|---|---|---|"]  # fmt: skip
        for r in (r for r in results if r["vertical"] == vertical):
            tools = ", ".join(dict.fromkeys(t for turn in r.get("turns", []) for t in turn.get("tools", []))) or "—"
            tin, tout = _tokens(r)
            lines.append(f"| `{r['id']}` {r['title']} | {STATUS_ICON[r['status']]} | {r.get('mode', '')} | "
                         f"{_model_label(r, primary)} | {tools} | {tin:,}/{tout:,} | {_latency(r) / 1000:.1f}s |")  # fmt: skip

    bad = [r for r in results if r["status"] != "pass"]
    if bad:
        lines += ["", "## Failed assertions", ""]
        for r in bad:
            note = " (explained in FAILURES.md)" if r["key"] in explained else ""
            lines.append(f"### `{r['key']}`{note}")
            lines += [f"- {f}" for f in r.get("failures", [])]
            for i, t in enumerate(r.get("turns", []), 1):
                reply = " ".join(t.get("reply", "").split())
                lines.append(f"  - turn {i}: _{t['request'][:120]}_ → {reply[:300] or '(no reply)'}")
            lines.append("")

    judged = [r for r in results if r.get("judge_scores")]
    if judged:
        lines += ["", "## LLM-as-judge (informational, never decides pass/fail)", "",
                  "| Scenario | Groundedness | Helpfulness | Judge's reason |", "|---|---|---|---|"]  # fmt: skip
        for r in judged:
            j: dict[str, Any] = r["judge_scores"]
            lines.append(f"| `{r['key']}` | {j.get('groundedness', '—')} | {j.get('helpfulness', '—')} | "
                         f"{str(j.get('reason', j.get('error', ''))).replace('|', '/')[:200]} |")  # fmt: skip
    (out / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
