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

# Casual, indirect phrasings that must stay answerable (recall). Only answerability is
# asserted here: these are vague, so the exact top chunk isn't pinned.
CASUAL = [
    ("store", "can I send it back"),
    ("store", "how long till it gets here"),
    ("store", "is the green one good for mornings"),
    ("store", "can i pay when it arrives"),
    ("booking", "what if I'm late"),
    ("booking", "my back hurts what should I book"),
    ("booking", "sprained my ankle playing football"),
    ("booking", "can I move my appointment"),
]

# Clearly off-topic: the cutoff sits just above the highest of these (see KB_MIN_COSINE).
# Near-domain unanswerables ("do you sell coffee") are deliberately not tested here: they
# pass the cutoff by design and are left to the model and the eval suite.
OUT_OF_SCOPE = [
    "What's the capital of Peru?",
    "Who won the 2018 football World Cup?",
    "How do I fix a flat bicycle tyre?",
    "Write me a python function to sort a list",
    "What is the boiling point of mercury?",
]


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


@pytest.mark.parametrize(("vertical", "question"), CASUAL)
async def test_casual_question_is_answerable(seeded: dict[str, uuid.UUID], vertical: str, question: str) -> None:
    res = await search_kb(customer(seeded[vertical]), question)
    assert res.answerable, (question, res.top_cosine)


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
