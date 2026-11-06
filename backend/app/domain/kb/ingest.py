"""KB ingestion for `make seed`: lint, chunk, check catalogs, embed, insert.

Everything that can fail (lint, format, catalog mismatch, embedding) happens in
prepare_kb(), before the seeder opens its database transaction.
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.kb.chunker import Chunk, Document, KBFormatError, load_kb_dir
from app.domain.kb.embeddings import embed_passages
from app.domain.kb.lint import lint_kb_dir


@dataclass(frozen=True, slots=True)
class PreparedKB:
    chunks: list[Chunk]
    vectors: list[str]  # pgvector text, aligned with chunks


def check_catalogs(docs: list[Document], catalogs: dict[str, dict[str, str | None]]) -> None:
    """Catalog headings must equal data.yaml names exactly, in both directions.

    catalogs maps a table ('products', 'services') to {name: sku or None}. When a sku is
    given, the entry must also state it as `SKU: <sku>`, so the agent can match KB text to
    catalogue rows. A table with rows but no catalog document is an error too.
    """
    covered = {d.catalog_of for d in docs if d.catalog_of}
    for table, entries in catalogs.items():
        if entries and table not in covered:
            raise KBFormatError(f"no KB document has catalog_of: {table}")
    for d in docs:
        if not d.catalog_of:
            continue
        expected = catalogs.get(d.catalog_of, {})
        headings = {c.heading: c for c in d.chunks}
        missing, extra = sorted(expected.keys() - headings), sorted(headings.keys() - expected)
        if missing or extra:
            raise KBFormatError(
                f"{d.doc_id}: catalog headings differ from data.yaml {d.catalog_of}: "
                f"missing {missing}, unknown {extra}"
            )
        for name, sku in expected.items():
            if sku and f"SKU: {sku}" not in headings[name].content:
                raise KBFormatError(f"{d.doc_id}#{headings[name].section}: expected 'SKU: {sku}'")


async def prepare_kb(kb_dir: Path, catalogs: dict[str, dict[str, str | None]]) -> PreparedKB:
    lint_kb_dir(kb_dir)
    docs = load_kb_dir(kb_dir)
    check_catalogs(docs, catalogs)
    chunks = [c for d in docs for c in d.chunks]
    return PreparedKB(chunks, await embed_passages([c.embed_text for c in chunks]))


async def insert_chunks(conn: AsyncConnection, org_id: UUID, kb: PreparedKB) -> None:
    """Insert into kb_chunks. The caller's transaction has app.org_id set (FORCE RLS)."""
    await conn.execute(
        text(
            "INSERT INTO kb_chunks (org_id, doc_id, doc_title, section, content, embedding) "
            "VALUES (:org_id, :doc_id, :doc_title, :section, :content, CAST(:embedding AS vector))"
        ),
        [
            {
                "org_id": org_id, "doc_id": c.doc_id, "doc_title": c.doc_title,
                "section": c.section, "content": c.content, "embedding": v,
            }  # fmt: skip
            for c, v in zip(kb.chunks, kb.vectors, strict=True)
        ],
    )
