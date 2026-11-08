"""KB text must agree with data.yaml on opening hours and on each org's canonical facts."""

from pathlib import Path

import pytest

from app.config import get_settings
from app.domain.kb.consistency import check
from app.seed.data import load_seed_data
from app.tenancy.orgs import load_org_configs

SEED = Path(get_settings().seed_dir)
BOOKING = next(c for c in load_org_configs(SEED) if c.vertical == "booking")
STORE = next(c for c in load_org_configs(SEED) if c.vertical == "store")


def _kb(tmp_path: Path, text: str) -> Path:
    (tmp_path / "doc.md").write_text(f"---\ndoc_id: doc\n---\n\n# Doc\n\n{text}\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("cfg", load_org_configs(SEED), ids=lambda c: c.slug)
def test_every_seed_kb_is_consistent(cfg) -> None:  # noqa: ANN001
    data = load_seed_data(SEED / cfg.slug / "data.yaml", cfg.vertical)
    assert check(SEED / cfg.slug / "kb", data) == []


@pytest.mark.parametrize(
    "text",
    [
        "We are closed on Sundays.",  # the studio opens on Sunday in data.yaml
        "We are open Sunday to Thursday.",  # it opens on Friday too
        "We are open 08:00 to 19:00 every day.",  # no day opens at 08:00
        "Cancelling more than 48 hours ahead is free.",  # canonical: 24 hours
        "A late cancellation costs a fee of 30% of the price.",  # canonical: 50%
    ],
)
def test_a_contradiction_with_the_booking_orgs_data_is_flagged(tmp_path: Path, text: str) -> None:
    data = load_seed_data(SEED / BOOKING.slug / "data.yaml", BOOKING.vertical)
    assert check(_kb(tmp_path, text), data), text


def test_a_wrong_return_window_is_flagged_and_the_right_one_passes(tmp_path: Path) -> None:
    data = load_seed_data(SEED / STORE.slug / "data.yaml", STORE.vertical)
    wrong = next(f for f in data.canonical_facts if f.mentions == "unopened").value
    other = "seven days" if wrong != "seven days" else "fourteen days"
    assert check(_kb(tmp_path, f"Unopened pouches can be returned within {other} of delivery."), data)
    assert check(_kb(tmp_path, f"Unopened pouches can be returned within {wrong} of delivery."), data) == []
