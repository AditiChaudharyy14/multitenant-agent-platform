"""`make kb-search ORG=<slug> Q="..."`: run the agent's KB search by hand.

Runs in the backend container as app_user, through the same search_kb the agent uses,
as an anonymous customer of that org. The slug only picks the org, like create_admin.
"""

import argparse
import asyncio
import sys

from app.db.session import TenantContext, app_engine
from app.domain.kb.search import KB_MIN_COSINE, search_kb
from app.tenancy.orgs import get_org_by_slug


async def run(slug: str, query: str, k: int, all_scores: bool) -> int:
    async with app_engine.connect() as conn:
        org = await get_org_by_slug(conn, slug)
    if org is None:
        print(f"unknown org {slug!r}", file=sys.stderr)
        return 2
    ctx = TenantContext(org_id=org.id, user_id=None, role="customer")
    res = await search_kb(ctx, query, k, min_cosine=-1.0 if all_scores else KB_MIN_COSINE)
    top = f"{res.top_cosine:.3f}" if res.top_cosine is not None else "n/a"
    answerable = (res.top_cosine or -1) >= KB_MIN_COSINE
    print(f"org={slug}  query={query!r}")
    print(f"top cosine={top}  threshold={KB_MIN_COSINE}  answerable={answerable}")
    for i, h in enumerate(res.results, start=1):
        body = " ".join(h.content.split("\n", 1)[-1].split())  # skip the heading line
        print(f"{i:>2}. rrf={h.score:.4f} cos={h.cosine:.3f}  {h.citation:<50} {body[:70]}")
    await app_engine.dispose()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--org", required=True)
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--all-scores", action="store_true", help="ignore the threshold")
    p.add_argument("query")
    a = p.parse_args()
    sys.exit(asyncio.run(run(a.org, a.query, a.k, a.all_scores)))


if __name__ == "__main__":
    main()
