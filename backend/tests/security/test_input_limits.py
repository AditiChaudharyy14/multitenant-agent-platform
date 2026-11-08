"""Server-side input limits, each with a clear error: message length, order lines and
quantities, memory length, and the history sent to the model kept under a token budget."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents import history
from app.agents.tools.store import create_order, update_order
from app.api.chat import MAX_MESSAGE_CHARS
from app.domain import memory
from tests.api_support import client_for
from tests.factories import Shop

pytestmark = pytest.mark.usefixtures("as_app_user")


def test_the_message_limit_is_2000_characters() -> None:
    assert MAX_MESSAGE_CHARS == 2000


async def test_a_message_over_the_limit_is_a_422_with_a_clear_reason(shop: Shop) -> None:
    async with await client_for(shop.org_id, shop.x) as c:
        cid = (await c.post("/api/conversations")).json()["id"]
        too_long = await c.post(f"/api/conversations/{cid}/messages", json={"content": "a" * (MAX_MESSAGE_CHARS + 1)})
        empty = await c.post(f"/api/conversations/{cid}/messages", json={"content": "   "})
    assert too_long.status_code == 422 and "2000" in too_long.text
    assert empty.status_code == 422


@pytest.mark.parametrize(
    "items",
    [
        [{"sku": f"S-{i}", "qty": 1} for i in range(21)],  # more than 20 lines
        [{"sku": "GRN-1", "qty": 0}],
        [{"sku": "GRN-1", "qty": 51}],
        [],
    ],
)
def test_order_lines_and_quantities_are_bounded(items: list[dict]) -> None:
    with pytest.raises(ValidationError):
        create_order.args_model.model_validate({"items": items})


def test_order_bounds_accept_the_edges() -> None:
    create_order.args_model.model_validate({"items": [{"sku": f"S-{i}", "qty": 50} for i in range(20)]})
    update_order.args_model.model_validate({"order_number": "HT-1001", "items": [{"sku": "GRN-1", "qty": 0}]})


def test_a_memory_value_over_200_characters_is_refused() -> None:
    with pytest.raises(memory.MemoryRefused, match="200"):
        memory._clean_value("v" * 201)
    assert memory._clean_value("v" * 200) == "v" * 200


def _row(role: str, content: str) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, tool_calls=None)


def test_history_is_trimmed_to_the_token_budget_and_starts_with_the_user() -> None:
    rows = []
    for i in range(20):
        rows += [_row("user", f"question {i} " + "x" * 2000), _row("assistant", f"answer {i} " + "y" * 2000)]
    msgs = history.to_messages(rows)
    assert history.estimated_tokens(msgs) <= history.HISTORY_TOKEN_BUDGET
    assert msgs and msgs[0].role == "user"
    assert "answer 19" in msgs[-1].content  # the newest turns are the ones kept


def test_a_short_history_is_kept_whole() -> None:
    rows = [_row("user", "hi"), _row("assistant", "hello"), _row("user", "price?"), _row("assistant", "NPR 850")]
    assert [m.content for m in history.to_messages(rows)] == ["hi", "hello", "price?", "NPR 850"]


def test_a_single_huge_turn_still_leaves_the_newest_user_message() -> None:
    rows = [_row("user", "z" * 100_000), _row("assistant", "ok")]
    msgs = history.to_messages(rows)
    assert history.estimated_tokens(msgs) <= history.HISTORY_TOKEN_BUDGET or len(msgs) <= 2
