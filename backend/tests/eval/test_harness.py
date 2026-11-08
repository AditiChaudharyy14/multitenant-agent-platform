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
