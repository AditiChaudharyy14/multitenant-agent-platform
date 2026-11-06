"""Chunking rules from CLAUDE.md: one FAQ Q&A per chunk, one catalog entry per chunk,
policies split at ## with overlap only for sections over 400 tokens."""

from pathlib import Path

import pytest
import yaml

from app.config import get_settings
from app.domain.kb.chunker import (
    MAX_SECTION_TOKENS,
    KBFormatError,
    chunk_document,
    count_tokens,
    load_kb_dir,
    slugify,
)

SEED = Path(get_settings().seed_dir)


def doc(chunking: str, body: str, extra: str = "") -> str:
    return f"---\ndoc_id: d\ntitle: Doc Title\nchunking: {chunking}\n{extra}---\n\n# Doc Title\n\n{body}"


def test_slugify_matches_github_anchors() -> None:
    assert slugify("How long does delivery take?") == "how-long-does-delivery-take"
    assert slugify("Dr. Prakash Thapa") == "dr-prakash-thapa"
    assert slugify("Cancelling within 24 hours") == "cancelling-within-24-hours"


def test_faq_is_one_chunk_per_question() -> None:
    d = chunk_document(doc("faq", "### Q one?\n\nA one.\n\n### Q two?\n\nA two.\n"))
    assert [c.section for c in d.chunks] == ["q-one", "q-two"]
    assert d.chunks[0].content == "Q one?\n\nA one."
    assert d.chunks[0].citation == "d#q-one"


def test_catalog_is_one_chunk_per_entry() -> None:
    d = chunk_document(doc("catalog", "Intro text.\n\n## Alpha\n\nA.\n\n## Beta\n\nB.\n"))
    assert [c.heading for c in d.chunks] == ["Alpha", "Beta"]


def test_short_sections_are_not_split_or_overlapped() -> None:
    d = chunk_document(doc("sections", "## One\n\nShort.\n\n## Two\n\nAlso short.\n"))
    assert [c.section for c in d.chunks] == ["one", "two"]
    assert "Short." not in d.chunks[1].content


def test_long_section_is_windowed_with_overlap() -> None:
    words = [f"w{i}" for i in range(600)]
    d = chunk_document(doc("sections", "## Long\n\n" + " ".join(words) + "\n\n## Short\n\nTiny.\n"))
    parts = [c for c in d.chunks if c.section.startswith("long")]
    assert len(parts) >= 2
    assert [c.section for c in parts] == [f"long-part-{i}" for i in range(1, len(parts) + 1)]
    for c in parts:
        assert count_tokens(c.content) <= MAX_SECTION_TOKENS
    # Consecutive windows share words (the overlap), and together they cover everything.
    first, second = (set(c.content.split()) for c in parts[:2])
    assert first & second
    assert set(words) <= set().union(*(set(c.content.split()) for c in parts))
    assert [c.section for c in d.chunks if not c.section.startswith("long")] == ["short"]


def test_html_comments_are_stripped() -> None:
    d = chunk_document(doc("faq", "### Q?\n\nAnswer.\n<!-- secret marker -->\nMore.\n"))
    assert "secret marker" not in d.chunks[0].content
    assert "More." in d.chunks[0].content


def test_duplicate_sections_are_rejected() -> None:
    with pytest.raises(KBFormatError):
        chunk_document(doc("faq", "### Same?\n\nA.\n\n### Same?\n\nB.\n"))


@pytest.mark.parametrize("bad", ["---\ntitle: x\nchunking: faq\n---\n### Q\n\nA\n", "no front matter"])
def test_front_matter_is_required(bad: str) -> None:
    with pytest.raises(KBFormatError):
        chunk_document(bad)


def test_unknown_chunking_is_rejected() -> None:
    with pytest.raises(KBFormatError):
        chunk_document(doc("paragraphs", "## A\n\nB\n"))


@pytest.mark.parametrize("kb", sorted(SEED.glob("*/kb")), ids=lambda p: p.parent.name)
def test_seed_kb_structure(kb: Path) -> None:
    docs = {d.doc_id: d for d in load_kb_dir(kb)}
    faq = [d for d in docs.values() if d.chunking == "faq"]
    assert faq and all(len(d.chunks) >= 18 for d in faq)
    # Catalog docs have exactly one chunk per entry in data.yaml.
    data = yaml.safe_load((kb.parent / "data.yaml").read_text(encoding="utf-8"))
    for d in docs.values():
        if d.catalog_of:
            assert sorted(c.heading for c in d.chunks) == sorted(e["name"] for e in data[d.catalog_of])
    # The eval-fixture comment never reaches the index.
    assert not any("EVAL FIXTURE" in c.content for d in docs.values() for c in d.chunks)
