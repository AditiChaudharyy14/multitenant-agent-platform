"""Local sentence embeddings: BAAI/bge-small-en-v1.5 through fastembed (ONNX, CPU, 384-d).

No API key and no network after the first download, which is cached in the
fastembed_cache Docker volume (EMBED_CACHE_DIR). The same model embeds passages at seed
time and queries at runtime, so their vectors are comparable.
"""

import asyncio
import threading
from collections.abc import Sequence

from app.config import get_settings

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384  # must match kb_chunks.embedding vector(384)

_model = None
_lock = threading.Lock()


def _get_model():  # noqa: ANN202 (fastembed is imported lazily, so no type at module level)
    """Load the model once per process, on first use (a few seconds; never at import)."""
    global _model
    with _lock:  # two threads embedding at startup must not both load it
        if _model is None:
            from fastembed import TextEmbedding

            _model = TextEmbedding(MODEL_NAME, cache_dir=get_settings().embed_cache_dir)
        return _model


def is_loaded() -> bool:
    """Whether the model is in memory (the health check reports it)."""
    return _model is not None


async def warm_up() -> None:
    """Load the model and run one embed, off the event loop. Called from the app lifespan
    before the server accepts requests, so the first search_knowledge doesn't pay for the
    load (several seconds on a cold container; ~30 s seen under memory pressure)."""
    await asyncio.to_thread(_embed, ["warm up"])


def to_pgvector(vec: Sequence[float]) -> str:
    """pgvector's text format, so queries bind it as text and CAST(:v AS vector)."""
    if len(vec) != DIMENSIONS:
        raise ValueError(f"expected {DIMENSIONS} dimensions, got {len(vec)}")
    return "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]"


def _embed(texts: list[str]) -> list[str]:
    # fastembed returns L2-normalised vectors, so cosine similarity = dot product.
    return [to_pgvector(v) for v in _get_model().embed(texts)]


# Passage vectors by text, per process. The model is deterministic, so a reseed (the tests
# reseed many times) never needs to embed an unchanged chunk again.
_passages: dict[str, str] = {}


async def embed_passages(texts: list[str]) -> list[str]:
    """Embed KB chunks. CPU-bound, so it runs in a worker thread."""
    missing = list(dict.fromkeys(t for t in texts if t not in _passages))
    if missing:
        _passages.update(zip(missing, await asyncio.to_thread(_embed, missing), strict=True))
    return [_passages[t] for t in texts]


async def embed_query(text: str) -> str:
    """Embed a search query, as is. fastembed adds no BGE instruction prefix for this
    model; adding "Represent this sentence for searching relevant passages: " by hand made
    no meaningful difference on our KBs (in/out-of-scope gap 0.133 vs 0.125 without), so
    the simpler form stays."""
    return (await asyncio.to_thread(_embed, [text]))[0]
