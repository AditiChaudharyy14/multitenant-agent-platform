"""Hybrid KB search: pgvector cosine + Postgres full-text, merged by reciprocal rank fusion.

There is deliberately no org parameter. The org comes only from the TenantContext, which
tenant_tx turns into app.org_id; RLS on kb_chunks then hides every other org's rows from
both the vector and the text search.
"""

from dataclasses import asdict, dataclass
from uuid import UUID

from sqlalchemy import text

from app.db.session import TenantContext, tenant_tx
from app.domain.kb.embeddings import embed_query

RRF_K = 60  # standard RRF constant (CLAUDE.md): score = Σ 1 / (RRF_K + rank)
CANDIDATES = 20  # rows taken from each ranker before fusion
# Weighted RRF: score = VEC_WEIGHT/(k + vector rank) + FTS_WEIGHT/(k + text rank). Customers
# phrase things casually ("how do I stop my order"), where the embedding generalises and
# word overlap misleads, so the vector side counts double.
VEC_WEIGHT = 2.0
FTS_WEIGHT = 1.0

# Answerability gate on the best cosine similarity. RRF scores are rank-based and say
# nothing about absolute relevance, so the gate uses the raw cosine instead.
#
# Calibrated on the seed KBs (`make kb-search --all-scores`), per org:
#   15 casual/indirect in-scope questions ("can I send it back", "what if I'm late"):
#      0.592–0.810 over both orgs
#   10 near-domain unanswerables ("do you sell coffee", "do you offer acupuncture"):
#      0.597–0.748, the same range as in-scope, so no cosine cutoff can separate them
#   5 clearly off-topic questions ("capital of Peru", "boiling point of mercury"):
#      0.392–0.619
# The gate favours recall: it sits just above the off-topic maximum (0.619, mercury vs the
# tea brewing guide's "boiling water"). Near-domain unanswerables pass it on purpose; the
# model and the eval suite handle them. 27 of 30 casual in-scope questions pass; the
# misses ("where's my stuff" 0.592, "something smoky?" 0.612, "I want something really
# relaxing" 0.617) score below the off-topic max, and the first two are database-tool
# questions (order status, products) anyway.
KB_MIN_COSINE = 0.625

# One statement: both rankers over the RLS-filtered kb_chunks, fused with a FULL JOIN so a
# chunk found by only one ranker still scores. Ties break on id for stable output.
_SEARCH = text(
    """
WITH vec AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> CAST(:q AS vector), id) AS rnk
  FROM kb_chunks
  ORDER BY embedding <=> CAST(:q AS vector), id
  LIMIT :n
),
-- plainto_tsquery ANDs every term, and a natural question rarely contains only words
-- from one chunk, so the terms are OR-ed. NULLIF: a query of only stop words gives ''.
tsq AS (
  SELECT NULLIF(replace(plainto_tsquery('english', :text)::text, '&', '|'), '')::tsquery AS q
),
fts AS (
  SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, tsq.q) DESC, c.id) AS rnk
  FROM kb_chunks c, tsq
  WHERE tsq.q IS NOT NULL AND c.tsv @@ tsq.q
  ORDER BY ts_rank_cd(c.tsv, tsq.q) DESC, c.id
  LIMIT :n
)
SELECT c.id, c.doc_id, c.doc_title, c.section, c.content,
       1 - (c.embedding <=> CAST(:q AS vector)) AS cosine,
       -- Casts: asyncpg would otherwise infer the weights as bigint and divide as integers.
       COALESCE(CAST(:wv AS float8) / (:rrf_k + vec.rnk), 0)
         + COALESCE(CAST(:wf AS float8) / (:rrf_k + fts.rnk), 0) AS rrf
FROM vec
FULL JOIN fts ON fts.id = vec.id
JOIN kb_chunks c ON c.id = COALESCE(vec.id, fts.id)
ORDER BY rrf DESC, c.id
"""
)


@dataclass(frozen=True, slots=True)
class KBHit:
    chunk_id: UUID
    doc_id: str
    doc_title: str
    section: str
    content: str
    score: float  # RRF score: the ranking
    cosine: float  # semantic similarity: the answerability gate

    @property
    def citation(self) -> str:
        return f"{self.doc_id}#{self.section}"


@dataclass(frozen=True, slots=True)
class KBSearchResult:
    results: list[KBHit]
    answerable: bool
    top_cosine: float | None  # best cosine among all candidates, for calibration and logs

    def to_tool_result(self) -> dict:
        """What the LLM sees: citations it may use, and whether to say "I don't know"."""
        return {
            "results": [
                {
                    "citation": h.citation,
                    "doc_title": h.doc_title,
                    "content": h.content,
                    "score": round(h.score, 4),
                }
                for h in self.results
            ],
            "answerable": self.answerable,
        }

    def as_dict(self) -> dict:
        return asdict(self)


async def search_kb(ctx: TenantContext, query: str, k: int = 5, *, min_cosine: float = KB_MIN_COSINE) -> KBSearchResult:
    """Top-k chunks for `query` in ctx's org only. min_cosine is overridable for calibration."""
    query = query.strip()
    if not query:
        return KBSearchResult([], False, None)
    qvec = await embed_query(query)  # before the transaction: no connection held while CPU-bound
    async with tenant_tx(ctx) as conn:
        # With HNSW, rows hidden by RLS are filtered after the index scan; an iterative scan
        # keeps reading the index until enough of this org's rows are found, in exact order.
        await conn.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        rows = (
            await conn.execute(
                _SEARCH,
                {
                    "q": qvec,
                    "text": query,
                    "n": CANDIDATES,
                    "rrf_k": RRF_K,
                    "wv": VEC_WEIGHT,
                    "wf": FTS_WEIGHT,
                },  # fmt: skip
            )
        ).all()
    hits = [KBHit(r.id, r.doc_id, r.doc_title, r.section, r.content, float(r.rrf), float(r.cosine)) for r in rows]
    top = max((h.cosine for h in hits), default=None)
    # A text-only match with weak semantics (e.g. one shared word) is dropped by the gate too.
    kept = [h for h in hits if h.cosine >= min_cosine][:k]
    return KBSearchResult(kept, bool(kept), top)
