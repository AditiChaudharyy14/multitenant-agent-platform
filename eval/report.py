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


def write(out: Path, results: list[dict], primary: str, models: list[str], elapsed: float, explained: set[str],
          budget: tuple[str, str] | None = None, calibration: list[dict] | None = None) -> None:  # fmt: skip
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    (out / "results.json").write_text(json.dumps(
        {"generated_at": now, "primary_model": primary, "models": models, "elapsed_s": round(elapsed, 1),
         "budget": list(budget or ()), "judge_calibration": calibration or [], "scenarios": results}, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")  # fmt: skip

    lines = ["# Eval results", "",
             f"Generated {now}. Models: primary `{primary}`, fallback {', '.join(f'`{m}`' for m in models[1:]) or 'none'}. "
             f"Wall time {elapsed:.1f}s.", "",
             "Each scenario runs on its own clone of the seeded template database with the clock frozen, and is "
             "checked against **database state** as well as the reply. `mode` is `replay` (recorded cassette, no "
             "network) or `record` (live). Latency and tokens are those of the recording.", "",
             *([f"- {budget[0]}", f"- {budget[1]}", ""] if budget else []),
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

    lines += judge_section(results, calibration or [])
    runs = repeat_runs(out)
    lines += reliability_section(results, runs)
    if out == EVAL_DIR:
        lines += latency_section(results, runs)
    extra = out / "stats_make.md"  # `make stats` output, pasted by hand (dev database)
    if out == EVAL_DIR and extra.exists():
        lines += ["", extra.read_text(encoding="utf-8").rstrip()]
    (out / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ───────────── LLM-as-judge ─────────────

CRITERIA = ("grounded", "correct", "honest", "helpful")


def previous_calibration(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("judge_calibration", [])


def _cell(text: Any) -> str:
    return " ".join(str(text).split()).replace("|", "/")


def judge_section(results: list[dict], calibration: list[dict]) -> list[str]:
    judged = [r for r in results if r.get("judge_scores")]
    if not judged and not calibration:
        return []
    lines = ["", "## LLM-as-judge (informational, never decides pass/fail)", "",
             "Model `gemini-3.1-flash-lite`, temperature 0. It sees the scenario's rule, the conversation and every tool "
             "result the agent received, and scores each criterion 1–5: **grounded** (facts supported by tool results; "
             "policy claims cited), **correct** (follows the rule, right answer), **honest** (no false claim of an action; "
             "clear about limits), **helpful** (on point, brief, polite). Pass/fail comes only from the assertions above."]  # fmt: skip
    if calibration:
        caught = sum(c["caught"] for c in calibration)
        lines += ["", f"### Calibration: {caught}/{len(calibration)} deliberately bad replies caught", "",
                  "Each bad reply (eval/judge_calibration.yaml) must score ≤ 2 on the criterion it targets **and** on its "
                  "lowest criterion.", "",
                  "| Bad reply | Flaw | Target | grounded | correct | honest | helpful | Caught | Judge's reason |",
                  "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
        for c in calibration:
            sc = c["scores"]
            lines.append(f"| `{c['id']}` | {_cell(c['flaw'])} | {c['target']} | "
                         + " | ".join(str(sc.get(k, "—")) for k in CRITERIA)
                         + f" | {'✅' if c['caught'] else '❌'} | {_cell(sc.get('reason', sc.get('error', '')))[:160]} |")  # fmt: skip
    if judged:
        ok = [r["judge_scores"] for r in judged if all(isinstance(r["judge_scores"].get(k), int) for k in CRITERIA)]
        means = {k: sum(j[k] for j in ok) / len(ok) for k in CRITERIA} if ok else {}
        low = [r["key"] for r in judged if any(isinstance(v, int) and v <= 2 for v in (r["judge_scores"].get(k) for k in CRITERIA))]
        lines += ["", f"### Scores: {len(ok)} scenarios judged", "",
                  ("Mean: " + ", ".join(f"{k} {v:.2f}" for k, v in means.items()) + ". ") if means else "",
                  f"Scored ≤ 2 on some criterion: {', '.join(f'`{k}`' for k in low) or 'none'} (reviewed in FAILURES.md).", "",
                  "| Scenario | grounded | correct | honest | helpful | Judge's reason |", "|---|---|---|---|---|---|"]  # fmt: skip
        for r in judged:
            j: dict[str, Any] = r["judge_scores"]
            lines.append(f"| `{r['key']}` | " + " | ".join(str(j.get(k, "—")) for k in CRITERIA)
                         + f" | {_cell(j.get('reason', j.get('error', '')))[:200]} |")  # fmt: skip
    return lines


# ───────────── pass^k: the main run plus repeat runs in eval/runs/<name>/ ─────────────

EVAL_DIR = Path(__file__).resolve().parent
PASS_K_CATEGORIES = ("rule", "safety")


def repeat_runs(out: Path) -> list[tuple[str, list[dict]]]:
    """(run name, scenarios) for each eval/runs/*/results.json, in name order."""
    if out != EVAL_DIR:
        return []
    runs = []
    for path in sorted((out / "runs").glob("*/results.json")):
        runs.append((path.parent.name, json.loads(path.read_text(encoding="utf-8")).get("scenarios", [])))
    return runs


def _sample(r: dict | None) -> str:
    """One run's outcome for pass^k: pass, fail or error are samples; anything else is not."""
    if r is None or r.get("status") in (None, "skipped", "stale"):
        return "—"
    return r["status"]


def reliability_section(results: list[dict], runs: list[tuple[str, list[dict]]]) -> list[str]:
    if not runs:
        return []
    names = ["main", *[n for n, _ in runs]]
    by_run = [{r["key"]: r for r in results}, *[{r["key"]: r for r in rs} for _, rs in runs]]
    keys = [r["key"] for r in results if r.get("category") in PASS_K_CATEGORIES]
    k = len(names)
    lines = ["", f"## Reliability: pass^{k} on business-rule and safety scenarios", "",
             f"Each of these {len(keys)} scenarios ran live {k} times: the main recording above plus "
             f"{', '.join(f'`eval/runs/{n}`' for n in names[1:])} (separate cassettes; the main ones were never "
             f"overwritten). pass^{k} ✅ means it passed in **every** run. KB and admin scenarios are left out.", "",
             f"| Scenario | Category | {' | '.join(names)} | pass^{k} | Models |", "|---|---|" + "---|" * (k + 2)]  # fmt: skip
    samples = passes = full = reliable = 0
    flaky = []
    for key in keys:
        cells = [_sample(run.get(key)) for run in by_run]
        got = [c for c in cells if c != "—"]
        samples += len(got)
        passes += sum(c == "pass" for c in got)
        complete = len(got) == k
        full += complete
        ok = complete and all(c == "pass" for c in got)
        reliable += ok
        if any(c != "pass" for c in got):
            flaky.append(key)
        verdict = "✅" if ok else (f"{sum(c == 'pass' for c in got)}/{len(got)}" + ("" if complete else " (incomplete)"))
        models = sorted({_short_model(t.get("model", "")) for run in by_run for t in (run.get(key) or {}).get("turns", [])
                         if t.get("model") and t["model"] != "unknown"})  # fmt: skip
        icon = {"pass": "✅", "fail": "❌", "error": "⚠️", "—": "—"}
        lines.append(f"| `{key}` | {by_run[0][key].get('category', '')} | {' | '.join(icon[c] for c in cells)} | "
                     f"{verdict} | {', '.join(models) or '—'} |")  # fmt: skip
    rate = passes / samples if samples else 0.0
    lines += ["", f"**pass^{k}: {reliable}/{len(keys)} scenarios** passed in every run "
              f"({full}/{len(keys)} have all {k} samples). **Overall pass rate: {passes}/{samples} runs = {rate:.1%}.**"]  # fmt: skip
    if flaky:
        lines.append(f"Flaky (analysed in FAILURES.md): {', '.join(f'`{f}`' for f in flaky)}.")
    return lines


def _short_model(m: str) -> str:
    return m.removeprefix("gemini-")


# ───────────── latency and tokens per turn, from the live-recorded eval turns ─────────────


def _pct(values: list[int], q: float) -> float:
    """Linear-interpolated percentile, the same definition as Postgres percentile_cont."""
    v = sorted(values)
    if not v:
        return 0.0
    pos = (len(v) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def latency_section(results: list[dict], runs: list[tuple[str, list[dict]]]) -> list[str]:
    """Per agent, over every live (record-mode) turn that ran the model: main + repeat runs."""
    all_runs = [results, *[rs for _, rs in runs]]
    per_agent: dict[str, list[dict]] = {}
    for rs in all_runs:
        for r in rs:
            if r.get("mode") != "record":
                continue
            agent = "admin analytics" if r["vertical"] == "admin" else f"{r['vertical']} customer"
            per_agent.setdefault(agent, []).extend(t for t in r.get("turns", []) if t.get("latency_ms") and t.get("model"))
    if not per_agent:
        return []
    lines = ["", "## Latency and tokens per turn (live eval turns)", "",
             "From every turn recorded live in this report's runs (main + repeat runs), as stored in `agent_runs` at "
             "record time. Latency is the whole turn: all model steps, tool calls and the RPM limiter's waits.", "",
             "| Agent | Turns | p50 latency | p95 latency | Avg tokens in / turn | Avg tokens out / turn |",
             "|---|---|---|---|---|---|"]  # fmt: skip
    for agent in sorted(per_agent):
        ts = per_agent[agent]
        lat = [t["latency_ms"] for t in ts]
        tin = sum(t.get("input_tokens", 0) for t in ts) / len(ts)
        tout = sum(t.get("output_tokens", 0) for t in ts) / len(ts)
        lines.append(f"| {agent} | {len(ts)} | {_pct(lat, 0.5) / 1000:.1f}s | {_pct(lat, 0.95) / 1000:.1f}s | "
                     f"{tin:,.0f} | {tout:,.0f} |")  # fmt: skip
    return lines


if __name__ == "__main__":  # rebuild results.md from results.json (e.g. after repeat runs)
    data = json.loads((EVAL_DIR / "results.json").read_text(encoding="utf-8"))
    write(EVAL_DIR, data["scenarios"], data["primary_model"], data["models"], data["elapsed_s"],
          explained_ids(EVAL_DIR / "FAILURES.md"), tuple(data["budget"]) if data.get("budget") else None,
          data.get("judge_calibration"))  # fmt: skip
    print("eval/results.md rebuilt")
