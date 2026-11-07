"""Optional LLM-as-judge (--judge) for KB-answer scenarios. Informational only.

It sees the customer's question, the knowledge-base passages the agent retrieved in that
turn, and the agent's answer, and scores groundedness and helpfulness 1-5. Its calls go
through the same record/replay cassettes (eval/cassettes/judge/<vertical>/<id>.json), so
`make eval --judge` stays offline once recorded.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from app.agents.providers import _live
from app.agents.providers.base import ChatRequest, Message, TextDelta
from app.agents.providers.recording import RecordReplayProvider, cassette_scope

SYSTEM = """You grade a customer-service assistant's answer. You are given the customer's \
question, the knowledge-base passages the assistant retrieved, and its answer.
Score two things from 1 (bad) to 5 (excellent):
- groundedness: every factual claim in the answer is supported by the passages (5), or \
the answer invents facts not in them (1). Saying "I don't know" when the passages lack the \
answer is fully grounded.
- helpfulness: the answer addresses the question clearly and briefly.
Reply with JSON only: {"groundedness": n, "helpfulness": n, "reason": "<one sentence>"}"""


async def score(sc: Any, r: dict, providers: Any, mode: str) -> dict:
    question = "\n".join(t["request"] for t in r["turns"])
    answer = r["turns"][-1]["reply"]
    prompt = f"QUESTION:\n{question}\n\nPASSAGES:\n{r.get('kb_passages') or '(none retrieved)'}\n\nANSWER:\n{answer}"
    req = ChatRequest(SYSTEM, [Message("user", prompt)], temperature=0.0, max_output_tokens=200)
    judge_model = os.environ.get("EVAL_JUDGE_MODEL", "gemini-3.1-flash-lite")
    if mode == "replay":
        provider = RecordReplayProvider("replay", providers.replay.dir)
    else:
        s = providers.settings.model_copy(update={"llm_model_gemini": judge_model, "llm_fallback_models_gemini": ""})
        provider = RecordReplayProvider("record", providers.replay.dir, _live(s, "gemini", providers.main_engine))
    try:
        with cassette_scope(f"judge/{sc.key}"):
            out = "".join([e.text async for e in provider.stream(req) if isinstance(e, TextDelta)])
        m = re.search(r"\{.*\}", out, re.S)
        data = json.loads(m.group(0)) if m else {"error": f"unparseable: {out[:100]}"}
        return {k: data.get(k) for k in ("groundedness", "helpfulness", "reason", "error") if k in data}
    except Exception as exc:  # the judge never breaks the suite
        return {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:150]}"}
