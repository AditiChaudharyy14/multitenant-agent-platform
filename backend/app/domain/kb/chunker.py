"""Markdown → chunks, following the chunking rules in CLAUDE.md.

Each document declares its strategy in front matter (never inferred from its filename,
so a new org needs no code):

  chunking: faq       one chunk per `###` question and its answer
  chunking: catalog   one chunk per `##` entry (a product, service or practitioner)
  chunking: sections  one chunk per `##` section; a section over MAX_SECTION_TOKENS is
                      split into overlapping windows (the only case with overlap)

A catalog document may also say `catalog_of: products|services`; the seeder then checks
its headings against data.yaml. Every chunk is cited as `doc_id#section`, where section
is the GitHub-style anchor of its heading.
"""

import math
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

STRATEGIES = {"faq": "###", "catalog": "##", "sections": "##"}
CATALOG_TABLES = {"products", "services"}

# bge-small-en-v1.5 reads at most 512 tokens; keeping chunks under 400 leaves headroom
# for the title prefix and for words that split into several word pieces.
MAX_SECTION_TOKENS = 400
WINDOW_WORDS = 225  # ≈ 300 tokens by count_tokens
OVERLAP_WORDS = 45  # ≈ 60 tokens shared between consecutive windows

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


class KBFormatError(ValueError):
    """A KB file is malformed (front matter, headings or duplicate sections)."""


@dataclass(frozen=True, slots=True)
class Chunk:
    doc_id: str
    doc_title: str
    section: str  # anchor slug, unique within the document
    heading: str  # the heading text as written
    content: str  # heading + body, what the agent reads

    @property
    def citation(self) -> str:
        return f"{self.doc_id}#{self.section}"

    @property
    def embed_text(self) -> str:
        # The title gives short chunks context ("Returns and Refunds: Opened tea ...").
        return f"{self.doc_title}: {self.content}"


@dataclass(frozen=True, slots=True)
class Document:
    doc_id: str
    title: str
    chunking: str
    catalog_of: str | None
    chunks: tuple[Chunk, ...]


def slugify(heading: str) -> str:
    """GitHub-style anchor: lowercase, punctuation dropped, spaces to hyphens."""
    s = re.sub(r"[^\w\s-]", "", heading.lower())
    return re.sub(r"[\s_]+", "-", s.strip()).strip("-")


def count_tokens(text: str) -> int:
    """Deterministic estimate: English averages about 4 word-piece tokens per 3 words."""
    return math.ceil(len(text.split()) * 4 / 3)


def _front_matter(text: str, source: str) -> tuple[dict, str]:
    m = _FRONT_MATTER.match(text)
    if not m:
        raise KBFormatError(f"{source}: missing '---' front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    for key in ("doc_id", "title", "chunking"):
        if not meta.get(key):
            raise KBFormatError(f"{source}: front matter needs {key!r}")
    if meta["chunking"] not in STRATEGIES:
        raise KBFormatError(f"{source}: chunking must be one of {sorted(STRATEGIES)}")
    if meta.get("catalog_of") not in (None, *CATALOG_TABLES):
        raise KBFormatError(f"{source}: catalog_of must be one of {sorted(CATALOG_TABLES)}")
    return meta, text[m.end() :]


def _sections(body: str, marker: str) -> list[tuple[str, str]]:
    """(heading, body) pairs for headings of exactly this level; text before the first is dropped."""
    out: list[tuple[str, list[str]]] = []
    for line in body.splitlines():
        if line.startswith(marker + " "):
            out.append((line[len(marker) + 1 :].strip(), []))
        elif out and not re.match(rf"#{{1,{len(marker)}}} ", line):
            # Lines that are higher-level headings (e.g. `# Title`) are not body text.
            out[-1][1].append(line)
    return [(h, "\n".join(lines).strip()) for h, lines in out]


def _windows(words: list[str]) -> list[str]:
    """Overlapping word windows; the last one ends exactly at the final word."""
    step = WINDOW_WORDS - OVERLAP_WORDS
    starts = range(0, max(len(words) - OVERLAP_WORDS, 1), step)
    return [" ".join(words[s : s + WINDOW_WORDS]) for s in starts]


def chunk_document(text: str, source: str = "<string>") -> Document:
    meta, body = _front_matter(text, source)
    body = _COMMENT.sub("", body)  # authoring notes (e.g. eval-fixture markers) never get indexed
    doc_id, title, strategy = str(meta["doc_id"]), str(meta["title"]), meta["chunking"]
    chunks: list[Chunk] = []
    for heading, section_body in _sections(body, STRATEGIES[strategy]):
        if not section_body:
            raise KBFormatError(f"{source}: section {heading!r} is empty")
        slug = slugify(heading)
        if strategy == "sections" and count_tokens(section_body) > MAX_SECTION_TOKENS:
            # Only long policy sections overlap, so each part still has its neighbour's context.
            for i, part in enumerate(_windows(section_body.split()), start=1):
                chunks.append(
                    Chunk(doc_id, title, f"{slug}-part-{i}", f"{heading} (part {i})", f"{heading} (part {i})\n\n{part}")
                )
        else:
            chunks.append(Chunk(doc_id, title, slug, heading, f"{heading}\n\n{section_body}"))
    if not chunks:
        raise KBFormatError(f"{source}: no {STRATEGIES[strategy]} sections found")
    seen: set[str] = set()
    for c in chunks:
        if c.section in seen:
            raise KBFormatError(f"{source}: duplicate section {c.section!r}")
        seen.add(c.section)
    return Document(doc_id, title, strategy, meta.get("catalog_of"), tuple(chunks))


def load_kb_dir(kb_dir: str | Path) -> list[Document]:
    """Every *.md in kb_dir, chunked. doc_ids must be unique within the org."""
    docs = [chunk_document(p.read_text(encoding="utf-8"), str(p)) for p in sorted(Path(kb_dir).glob("*.md"))]
    if not docs:
        raise KBFormatError(f"{kb_dir}: no markdown files")
    ids = [d.doc_id for d in docs]
    if len(ids) != len(set(ids)):
        raise KBFormatError(f"{kb_dir}: duplicate doc_id in {ids}")
    return docs
