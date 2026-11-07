"""The eval scenario files are well-formed, cover both agents, and check database state.

Offline and fast: it only parses eval/scenarios (mounted read-only into the ops container)
and cross-checks the names it refers to against seed/<slug>/data.yaml.
"""

import re
import sys
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


def test_ids_are_unique() -> None:
    keys = [s.key for s in SCENARIOS]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.key)
def test_scenario_refers_to_real_seed_data(sc: Scenario) -> None:
    assert sc.org in ORGS, f"unknown org {sc.org}"
    assert ORGS[sc.org].vertical == sc.vertical, "scenario directory must match the org's vertical"
    data = _data(sc.org)
    emails = {u["email"] for u in data["users"]}
    assert sc.user in emails
    seeded = {("order", o["key"]) for o in data.get("orders", [])} | {("booking", b["key"]) for b in data.get("bookings", [])}
    text = yaml.safe_dump(sc.model_dump())
    for kind, key in re.findall(r"\{\{(order|booking):([\w-]+)\}\}", text):
        assert (kind, key) in seeded, f"placeholder {kind}:{key} is not a seed key"
    for db in sc.assertions.db:
        ref = (db.order and db.order.ref and ("order", db.order.ref)) or (db.booking and db.booking.ref and ("booking", db.booking.ref))
        if ref:
            assert ref in seeded, f"assertion refers to unknown seed key {ref}"
    for step in sc.setup:
        if step.add_booking:
            assert step.add_booking.customer in emails


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
