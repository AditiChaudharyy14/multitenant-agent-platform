"""Hybrid retrieval quality on the real seed KBs, searched as app_user."""

import uuid

import pytest

from app.db.session import TenantContext
from app.domain.kb.search import search_kb

pytestmark = pytest.mark.usefixtures("as_app_user")

# (vertical, question, expected citation). One verbatim FAQ question and one paraphrase each.
KNOWN = [
    ("store", "How long does delivery take?", "faq#how-long-does-delivery-take"),
    ("store", "can I send back a pouch of tea I already opened", "faq#can-i-return-an-opened-pack-of-tea"),
    ("booking", "What happens if I arrive late?", "faq#what-happens-if-i-arrive-late"),
    ("booking", "do you work on saturday", "faq#are-you-open-on-saturdays"),
]

OUT_OF_SCOPE = ["What's the capital of Peru?", "Who won the 2018 football World Cup?"]


def customer(org_id: uuid.UUID) -> TenantContext:
    return TenantContext(org_id=org_id, user_id=None, role="customer")


@pytest.mark.parametrize(("vertical", "question", "citation"), KNOWN)
async def test_known_question_in_top_3(
    seeded: dict[str, uuid.UUID], vertical: str, question: str, citation: str
) -> None:
    res = await search_kb(customer(seeded[vertical]), question)
    assert res.answerable
    top3 = [h.citation for h in res.results[:3]]
    assert citation in top3, top3


@pytest.mark.parametrize("vertical", ["store", "booking"])
@pytest.mark.parametrize("question", OUT_OF_SCOPE)
async def test_out_of_scope_is_not_answerable(
    seeded: dict[str, uuid.UUID], vertical: str, question: str
) -> None:
    res = await search_kb(customer(seeded[vertical]), question)
    assert res.answerable is False
    assert res.results == []
    assert res.to_tool_result() == {"results": [], "answerable": False}


async def test_results_carry_citation_fields(seeded: dict[str, uuid.UUID]) -> None:
    res = await search_kb(customer(seeded["store"]), "how should I brew oolong", k=3)
    assert 0 < len(res.results) <= 3
    hit = res.results[0]
    assert hit.citation == f"{hit.doc_id}#{hit.section}"
    assert hit.doc_title and hit.content and hit.score > 0
    scores = [h.score for h in res.results]
    assert scores == sorted(scores, reverse=True)
