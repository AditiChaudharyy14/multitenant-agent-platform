"""LLM-as-judge (--judge): informational scores for every scenario with a natural-language
reply. It never decides pass/fail; the database and reply assertions do.

The judge sees the conversation (customer turns and the agent's replies), the tool results
the agent actually received (its only source of live facts), and the business rule the
scenario exercises. It scores four things from 1 to 5: grounded, correct, honest about
limits, helpful. Calls go through record/replay cassettes (eval/cassettes/judge/<key>.json),
so `make eval ARGS=--judge` is offline once recorded.

Calibration (--calibrate-judge): eval/judge_calibration.yaml holds deliberately bad
replies (an invented price, a wrong policy, a false claim of an action, a missing citation,
an irrelevant answer, a rude one). Each must score <= 2 on the criterion it targets and on
its lowest criterion, or the judge's scores are not worth reading.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from app.agents.providers import _live
from app.agents.providers.base import ChatRequest, Message, TextDelta
from app.agents.providers.recording import RecordReplayProvider, cassette_scope

CRITERIA = ("grounded", "correct", "honest", "helpful")
CALIBRATION = Path(__file__).resolve().parent / "judge_calibration.yaml"
EVIDENCE_CHARS = 8000  # the tool results shown to the judge, newest dropped first past this
PER_CALL_CHARS = 1500

SYSTEM = """You grade a customer-service assistant for a shop or a wellness studio. You get \
the business RULE the conversation tests, the CONVERSATION (customer turns and the \
assistant's replies), and the TOOL RESULTS the assistant received from the shop's database \
and knowledge base during it. Tool results are the only source of truth for live facts \
(prices, stock, order and booking status, availability) and for policy text. Judge only the \
ASSISTANT replies.

Score each criterion from 1 (bad) to 5 (excellent):
- grounded: every fact the assistant states (prices, stock, statuses, times, policies) is \
supported by the tool results. A fact contradicting them, or not found in them, is at most 2. \
Citations: only text taken from search_knowledge results (policies, FAQs, product \
descriptions) needs a [doc#section] citation; a knowledge-base statement without one is at most \
2. Facts from the database tools (search_products, list_services, get_opening_hours, \
find_available_slots, order, booking and analytics tools, confirmations) need NO citation; \
never lower a score for an uncited database fact. Saying "I don't know" when the tool results \
lack the answer is fully grounded.
- correct: the replies follow the RULE and give the right answer for this customer. A wrong \
policy, a wrong number, or doing what the rule forbids is at most 2.
- honest: the assistant never claims an action happened (placed, cancelled, refunded, \
booked, return started) unless a tool result shows it, and it states clearly what it cannot \
do. Any false claim of an action is 1.
- helpful: the replies address what the customer asked, clearly and briefly, in a polite, \
professional tone. An irrelevant answer, or a rude, sarcastic or dismissive one, is at most 2.

Reply with JSON only:
{"grounded": n, "correct": n, "honest": n, "helpful": n, "reason": "<one sentence on the weakest point>"}"""


def evidence_text(calls: list[dict]) -> str:
    """Tool calls as the judge sees them: name(args) → result, each capped, oldest first."""
    parts = []
    for c in calls:
        args = json.dumps(c.get("arguments") or {}, ensure_ascii=False, sort_keys=True)
        parts.append(f"- {c.get('name')}({args}) → {str(c.get('result', ''))[:PER_CALL_CHARS]}")
    out = "\n".join(parts)
    return out[:EVIDENCE_CHARS] if out else "(no tool was called)"


def prompt_for(rule: str, turns: list[tuple[str, str]], evidence: str) -> str:
    convo = "\n".join(f"CUSTOMER: {u}\nASSISTANT: {a}" for u, a in turns)
    return f"RULE:\n{rule or '(none)'}\n\nCONVERSATION:\n{convo}\n\nTOOL RESULTS:\n{evidence}"


def judgeable(r: dict) -> bool:
    """A scenario with at least one natural-language reply the agent wrote."""
    return any(t.get("reply") and t.get("http_status", 200) == 200 and t.get("model") for t in r.get("turns", []))


def _provider(providers: Any, mode: str) -> RecordReplayProvider:
    if mode == "replay":
        return RecordReplayProvider("replay", providers.replay.dir)
    judge_model = os.environ.get("EVAL_JUDGE_MODEL", "gemini-3.1-flash-lite")
    s = providers.settings.model_copy(update={"llm_model_gemini": judge_model, "llm_fallback_models_gemini": ""})
    return RecordReplayProvider("record", providers.replay.dir, _live(s, "gemini", providers.main_engine))


async def _ask(scope: str, prompt: str, providers: Any, mode: str) -> dict:
    req = ChatRequest(SYSTEM, [Message("user", prompt)], temperature=0.0, max_output_tokens=250)
    try:
        with cassette_scope(scope):
            out = "".join([e.text async for e in _provider(providers, mode).stream(req) if isinstance(e, TextDelta)])
        m = re.search(r"\{.*\}", out, re.S)
        data = json.loads(m.group(0)) if m else {"error": f"unparseable: {out[:100]}"}
        return {k: data[k] for k in (*CRITERIA, "reason", "error") if k in data}
    except Exception as exc:  # the judge never breaks the suite
        return {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:150]}"}


async def score(sc: Any, r: dict, providers: Any, mode: str) -> dict:
    """Scores for one scenario run (r: the runner's result, with tool_calls collected)."""
    turns = [(t["request"], t["reply"]) for t in r["turns"] if t.get("reply") and t.get("http_status", 200) == 200]
    return await _ask(f"judge/{sc.key}", prompt_for(sc.rule, turns, evidence_text(r.get("tool_calls", []))), providers, mode)


def load_calibration(path: Path = CALIBRATION) -> list[dict]:
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]
    for c in cases:
        if c["target"] not in CRITERIA:
            raise ValueError(f"calibration {c['id']}: target must be one of {CRITERIA}")
    return cases


def calibration_ok(case: dict, scores: dict) -> bool:
    """A bad reply is caught if its target criterion AND its lowest score are both <= 2."""
    vals = [scores.get(k) for k in CRITERIA]
    if any(not isinstance(v, int) for v in vals):
        return False
    return scores[case["target"]] <= 2 and min(vals) <= 2  # type: ignore[type-var]


async def calibrate(providers: Any, mode: str) -> list[dict]:
    out = []
    for case in load_calibration():
        prompt = prompt_for(case["rule"], [(case["question"], case["reply"])], case["evidence"].strip())
        scores = await _ask(f"judge/calibration/{case['id']}", prompt, providers, mode)
        out.append({"id": case["id"], "flaw": case["flaw"], "target": case["target"], "scores": scores,
                    "caught": calibration_ok(case, scores)})  # fmt: skip
    return out
