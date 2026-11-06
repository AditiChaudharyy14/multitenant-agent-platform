"""The KB must never carry live facts (invariant 8): prices and stock come only from tools."""

from pathlib import Path

import pytest

from app.config import get_settings
from app.domain.kb.lint import KBLintError, lint_kb_dir, lint_text


@pytest.mark.parametrize(
    "line",
    [
        "Our blend costs NPR 450.",
        "Only Rs. 300 for a pouch",
        "Rs 300 per pack",
        "रु 200 मात्र",
        "₹150",
        "Just 500 rupees!",
        "Now $12 a tin",
        "Hurry, only 3 left in stock",
        "We have 12 units available",
        "2 remaining",
        "Stock: 40",
    ],
)
def test_price_or_stock_figures_are_rejected(line: str) -> None:
    assert lint_text(line), line


@pytest.mark.parametrize(
    "line",
    [
        "What happens if a tea is out of stock?",
        "Cancelling within 24 hours costs 50% of the service price.",
        "Steep for 3 minutes at 80°C.",
        "Delivery takes three to five working days.",
        "Prices and availability always come from our live catalogue.",
        "Our hours are 09:00 to 19:00.",
    ],
)
def test_ordinary_text_passes(line: str) -> None:
    assert lint_text(line) == [], line


def test_lint_dir_reports_file_and_line(tmp_path: Path) -> None:
    (tmp_path / "ok.md").write_text("---\ndoc_id: ok\ntitle: OK\n---\nFine.\n", encoding="utf-8")
    (tmp_path / "bad.md").write_text("---\ndoc_id: bad\ntitle: Bad\n---\n\nA pouch is NPR 450.\n")
    with pytest.raises(KBLintError) as ei:
        lint_kb_dir(tmp_path)
    assert "bad.md:6" in str(ei.value)
    assert "ok.md" not in str(ei.value)


def test_seed_knowledge_bases_are_clean() -> None:
    kb_dirs = sorted(Path(get_settings().seed_dir).glob("*/kb"))
    assert kb_dirs, "no seed kb directories found"
    for kb in kb_dirs:
        lint_kb_dir(kb)  # raises on any finding
