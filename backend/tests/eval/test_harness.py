"""Offline checks of the eval harness itself: scenario schema rules, the {{chars:N}}
placeholder, and the quota floor that protects the live-demo budget."""

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

EVAL_DIR = Path("/eval")
if not (EVAL_DIR / "scenario.py").exists():
    pytest.skip("eval/ is not mounted here", allow_module_level=True)
sys.path.insert(0, str(EVAL_DIR))

import checks  # noqa: E402
import quota  # noqa: E402
from scenario import Scenario, Turn, load_scenarios  # noqa: E402

SCENARIOS = load_scenarios(EVAL_DIR / "scenarios")
BASE = {"id": "x", "title": "t", "org": "himali-tea", "user": "kiran@himali.test", "category": "rule",
        "assertions": {"no_new_rows": ["orders"]}}  # fmt: skip


def test_every_category_is_used_and_admin_scenarios_are_admin() -> None:
    assert {s.category for s in SCENARIOS} == {"rule", "safety", "kb", "admin"}
    assert all((s.category == "admin") == (s.vertical == "admin") for s in SCENARIOS)


def test_category_is_required() -> None:
    raw = {k: v for k, v in BASE.items() if k != "category"} | {"turns": [{"user": "hi"}]}
    with pytest.raises(ValidationError, match="category"):
        Scenario.model_validate(raw)


def test_a_turn_can_carry_its_own_setup() -> None:
    t = Turn.model_validate({"user": "and now?", "setup": [{"set_price": {"sku": "HT-GRN-002", "price": "1350"}}]})
    assert t.setup[0].set_price is not None and t.setup[0].set_price.price == "1350"


def test_turn_expect_http_must_be_a_4xx() -> None:
    assert Turn.model_validate({"user": "x", "expect_http": 422}).expect_http == 422
    with pytest.raises(ValidationError, match="4xx"):
        Turn.model_validate({"user": "x", "expect_http": 200})


def test_a_scenario_refused_on_every_turn_needs_no_model() -> None:
    refused = Scenario.model_validate(BASE | {"turns": [{"user": "{{chars:2001}}", "expect_http": 422}]})
    talks = Scenario.model_validate(BASE | {"turns": [{"user": "{{chars:2001}}", "expect_http": 422}, {"user": "hi"}]})
    assert not refused.needs_model
    assert talks.needs_model


@pytest.mark.parametrize("n", [1, 43, 2000, 2001])
def test_chars_placeholder_expands_to_exactly_n_characters(n: int) -> None:
    out = checks.expand_chars(f"{{{{chars:{n}}}}}")
    assert len(out) == n and "{" not in out


def test_chars_placeholder_leaves_other_text_alone() -> None:
    assert checks.expand_chars("order {{order:c2-pending}} now") == "order {{order:c2-pending}} now"


@pytest.mark.parametrize(
    ("left", "reserve", "ok"),
    [
        (888, 0, True),
        (135, 0, True),  # 135 - 15 = 120: exactly at the floor
        (134, 0, False),  # one more scenario could end below 120
        (205, 70, True),
        (204, 70, False),
    ],
)
def test_quota_floor_keeps_the_demo_budget(left: int, reserve: int, ok: bool) -> None:
    assert quota.may_record(left, floor=120, reserve=reserve) is ok


def test_quota_left_counts_every_model_and_never_goes_negative() -> None:
    assert quota.left_combined({"primary": 12, "fallback": 0}, 450) == 888
    assert quota.left_combined({"primary": 460, "fallback": 400}, 450) == 50


# ───────────── pass^k and latency (eval/report.py) ─────────────

import report  # noqa: E402


def _r(
    key: str, status: str, category: str = "rule", latency: int = 1000, model: str = "gemini-3.5-flash-lite"
) -> dict:
    vertical, id_ = key.split("/")
    return {"key": key, "id": id_, "vertical": vertical, "category": category, "status": status, "mode": "record",
            "turns": [{"model": model, "latency_ms": latency, "input_tokens": 100, "output_tokens": 10}]}  # fmt: skip


def test_pass_k_needs_every_run_to_pass_and_skips_kb() -> None:
    main = [_r("store/a", "pass"), _r("store/b", "pass"), _r("store/kb", "fail", "kb")]
    runs = [("run2", [_r("store/a", "pass"), _r("store/b", "fail")]), ("run3", [_r("store/a", "pass")])]
    text = "\n".join(report.reliability_section(main, runs))
    assert "| `store/a` | rule | ✅ | ✅ | ✅ | ✅ |" in text
    assert "| `store/b` | rule | ✅ | ❌ | — | 1/2 (incomplete) |" in text
    assert "store/kb" not in text.split("Flaky")[0]
    assert "pass^3: 1/2 scenarios" in text and "Overall pass rate: 4/5 runs = 80.0%" in text
    assert "Flaky (analysed in FAILURES.md): `store/b`" in text


def test_no_repeat_runs_means_no_reliability_section() -> None:
    assert report.reliability_section([_r("store/a", "pass")], []) == []


def test_percentile_matches_postgres_percentile_cont() -> None:
    # SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY x) FROM unnest('{1,2,3,4,10}'::int[]) x  → 8.8
    assert report._pct([1, 2, 3, 4, 10], 0.95) == pytest.approx(8.8)
    assert report._pct([1, 2, 3, 4, 10], 0.5) == 3


def test_latency_counts_only_live_turns_per_agent() -> None:
    replayed = _r("store/c", "pass", latency=99_000) | {"mode": "replay"}
    text = "\n".join(report.latency_section([_r("store/a", "pass", latency=2000), replayed, _r("admin/x", "pass", "admin")],
                                            [("run2", [_r("store/a", "pass", latency=4000)])]))  # fmt: skip
    assert "| store customer | 2 | 3.0s | 3.9s | 100 | 10 |" in text
    assert "| admin analytics | 1 |" in text


# ───────────── LLM-as-judge (eval/judge.py): calibration set and verdict rule ─────────────

import judge  # noqa: E402


def test_calibration_has_six_bad_replies_covering_every_criterion() -> None:
    cases = judge.load_calibration()
    assert {c["id"] for c in cases} == {"invented_price", "wrong_policy", "false_action", "missing_citation",
                                        "irrelevant", "rude"}  # fmt: skip
    assert {c["target"] for c in cases} == set(judge.CRITERIA)


@pytest.mark.parametrize(
    ("scores", "caught"),
    [
        ({"grounded": 1, "correct": 3, "honest": 5, "helpful": 4}, True),
        ({"grounded": 3, "correct": 1, "honest": 5, "helpful": 4}, False),  # target not low
        ({"grounded": 2, "correct": 5, "honest": 5, "helpful": 5}, True),
        ({"grounded": None, "correct": 1, "honest": 1, "helpful": 1}, False),  # unparseable is never "caught"
        ({"error": "RateLimited"}, False),
    ],
)
def test_calibration_verdict(scores: dict, caught: bool) -> None:
    assert judge.calibration_ok({"target": "grounded"}, scores) is caught


def test_only_replies_the_agent_wrote_are_judged() -> None:
    assert judge.judgeable({"turns": [{"reply": "Hi", "model": "m", "http_status": 200}]})
    assert not judge.judgeable(
        {"turns": [{"reply": "String should have at most 2000 characters", "model": "", "http_status": 422}]}
    )
    assert not judge.judgeable({"turns": []})


def test_judge_evidence_is_capped() -> None:
    calls = [{"name": "search_knowledge", "arguments": {"query": "q"}, "result": "x" * 5000}] * 10
    out = judge.evidence_text(calls)
    assert len(out) <= judge.EVIDENCE_CHARS and out.startswith("- search_knowledge")


# ───────────── stricter assertions added after the final recording ─────────────


@pytest.mark.parametrize(
    ("reply", "found"),
    [
        ("Try our Jasmine Pearl Green instead.", "Jasmine Pearl Green"),
        ("you might want to try one of our other green teas", None),  # "other" alone is not an alternative
        ("Kanchenjunga Mist Green is short", None),  # the short item itself is not in the candidate list
        ("jasmine pearl green (25 in stock)", "Jasmine Pearl Green"),
    ],
)
def test_reply_must_name_a_real_alternative(reply: str, found: str | None) -> None:
    assert checks.names_one_of(reply, ["Ilam First Flush Green", "Jasmine Pearl Green"]) == found


def test_add_order_setup_step_validates() -> None:
    from scenario import SetupStep

    step = SetupStep.model_validate({"add_order": {"customer": "anisha@himali.test", "items": {"HT-CHA-001": 3},
                                                   "days_ago": 30, "address": "House 14"}})  # fmt: skip
    assert step.add_order is not None and step.add_order.status == "delivered"
    with pytest.raises(ValidationError):
        SetupStep.model_validate({"add_order": {"customer": "x", "items": {}, "days_ago": 0, "address": "y"}})
