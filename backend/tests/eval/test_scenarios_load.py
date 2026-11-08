"""The eval scenario files are well-formed, cover both agents, and check database state.

Offline and fast: it only parses eval/scenarios (mounted read-only into the ops container)
and cross-checks the names it refers to against seed/<slug>/data.yaml.
"""

import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

EVAL_DIR = Path("/eval")
if not (EVAL_DIR / "scenario.py").exists():
    pytest.skip("eval/ is not mounted here", allow_module_level=True)
sys.path.insert(0, str(EVAL_DIR))

from scenario import Scenario, load_scenarios  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.tenancy.orgs import load_org_configs  # noqa: E402

SCENARIOS = load_scenarios(EVAL_DIR / "scenarios")
SEED = Path(get_settings().seed_dir)
ORGS = {c.slug: c for c in load_org_configs(SEED)}


def _data(slug: str) -> dict:
    return yaml.safe_load((SEED / slug / "data.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("vertical", ["store", "booking"])
def test_at_least_16_scenarios_per_agent(vertical: str) -> None:
    assert sum(s.vertical == vertical for s in SCENARIOS) >= 16


def test_at_least_6_admin_scenarios_including_a_403() -> None:
    admin = [s for s in SCENARIOS if s.vertical == "admin"]
    assert len(admin) >= 6
    assert all(s.chat == "admin" for s in admin)
    assert any(s.expect_http == 403 for s in admin)
    assert {ORGS[s.org].vertical for s in admin} == {"store", "booking"}


def test_ids_are_unique() -> None:
    keys = [s.key for s in SCENARIOS]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.key)
def test_scenario_refers_to_real_seed_data(sc: Scenario) -> None:
    assert sc.org in ORGS, f"unknown org {sc.org}"
    # Admin-chat scenarios live in scenarios/admin/; the others in their org's vertical.
    expected_dir = "admin" if sc.chat == "admin" else ORGS[sc.org].vertical
    assert sc.vertical == expected_dir, f"scenario belongs in scenarios/{expected_dir}/"
    data = _data(sc.org)
    emails = {u["email"] for u in data["users"]}
    assert sc.user in emails
    seeded = {("order", o["key"]) for o in data.get("orders", [])} | {
        ("booking", b["key"]) for b in data.get("bookings", [])
    }
    text = yaml.safe_dump(sc.model_dump())
    for kind, key in re.findall(r"\{\{(order|booking):([\w-]+)\}\}", text):
        assert (kind, key) in seeded, f"placeholder {kind}:{key} is not a seed key"
    for db in sc.assertions.db:
        ref = (db.order and db.order.ref and ("order", db.order.ref)) or (
            db.booking and db.booking.ref and ("booking", db.booking.ref)
        )
        if ref:
            assert ref in seeded, f"assertion refers to unknown seed key {ref}"
    for step in [*sc.setup, *(s for t in sc.turns for s in t.setup)]:
        if step.add_booking:
            assert step.add_booking.customer in emails
    if sc.chat == "admin":
        role = next(u["role"] for u in data["users"] if u["email"] == sc.user)
        # The admin chat is driven by an admin, except the scenarios proving it refuses others.
        assert role == ("admin" if sc.expect_http is None else "customer")


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.key)
def test_every_scenario_checks_database_state(sc: Scenario) -> None:
    """Reply text alone proves nothing about what the agent did."""
    assert sc.assertions.checks_state()


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.key)
def test_regexes_compile(sc: Scenario) -> None:
    a = sc.assertions
    patterns = [*a.reply_matches, *a.reply_not_matches, *a.any_reply_not_matches]
    patterns += [p for t in sc.turns if t.expect for p in (*t.expect.reply_matches, *t.expect.reply_not_matches)]
    for p in patterns:
        re.compile(p)


# ───────────── the ground-truth number matcher (eval/checks.py) ─────────────

import checks  # noqa: E402


@pytest.mark.parametrize(
    ("reply", "value", "found"),
    [
        ("Revenue today was NPR 4,450.00 from 3 orders.", Decimal("4450.00"), True),
        ("Revenue today was NPR 4450 from 3 orders.", Decimal("4450.00"), True),
        ("Revenue today was NPR 14,450.00.", Decimal("4450.00"), False),
        ("3 orders", 3, True),
        ("3.5% of conversations", 3, False),
        ("13 orders", 3, False),
        ("Conversion was 69.2% this week.", Decimal("69.2"), True),
        ("Conversion was 69.25% this week.", Decimal("69.2"), False),
        ("Sunita Rai is the busiest.", "Sunita Rai", True),
        ("Maya Tamang is the busiest.", "Sunita Rai", False),
    ],
)
def test_reply_has_value(reply: str, value: object, found: bool) -> None:
    assert checks.reply_has_value(reply, value) is found
