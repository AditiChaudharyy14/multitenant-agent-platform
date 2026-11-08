"""GET /api/kb/source?ref=<doc_id#section>: one KB chunk, for the citation drawer.

The ref names a chunk but never an org: the org comes only from the session, and RLS on
kb_chunks hides every other org's rows. A foreign ref, a missing one and a malformed one
all get the same 404 body, so refs can't be used to probe another org's KB.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from app.api.conversations import not_found
from app.auth.deps import current_ctx
from app.db.session import tenant_tx
from app.tenancy.context import ToolContext

router = APIRouter(prefix="/api/kb")

REF_MAX = 300  # longer than any doc_id#section the chunker produces


@router.get("/source")
async def kb_source(
    ref: str = Query(max_length=REF_MAX * 2), ctx: ToolContext = Depends(current_ctx)
) -> dict[str, Any]:
    doc_id, sep, section = ref.rpartition("#")
    if not sep or not doc_id or not section or len(ref) > REF_MAX:
        raise not_found()
    async with tenant_tx(ctx.tenant) as conn:
        row = (
            await conn.execute(
                text("SELECT doc_id, doc_title, section, content FROM kb_chunks WHERE doc_id = :d AND section = :s"),
                {"d": doc_id, "s": section},
            )
        ).one_or_none()
    if row is None:
        raise not_found()
    return {"ref": ref, **row._mapping}
