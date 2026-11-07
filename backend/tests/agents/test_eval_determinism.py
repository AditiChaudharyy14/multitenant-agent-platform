"""Everything the server feeds back to the model must be reproducible under evals.

EVAL_MODE=replay matches each request byte for byte, so proposal ids (in tool results) and
turn ids (in server-action call ids, replayed through history) come from new_id(), which
deterministic_ids() makes repeatable. And the daily budget can be pinned to an engine, so
the eval runner counts llm_usage in the main database while the app runs on a clone.
"""

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from app.agents.ids import deterministic_ids, new_id
from app.agents.providers import get_provider
from app.agents.tools.store import create_order
from app.config import get_settings
from app.db.session import tenant_tx
from app.domain.conversations import repo as conversations
from tests.factories import Shop
from tests.store_support import call, proposal_id, turn_ctx

pytestmark = pytest.mark.usefixtures("as_app_user")


async def _propose(shop: Shop) -> uuid.UUID:
    ctx = turn_ctx(shop, shop.x)
    result = await call(create_order, ctx, items=[{"sku": "GRN-1", "qty": 1}], delivery_address="House 7, Kathmandu")
    return proposal_id(result)


async def test_proposal_id_comes_from_new_id(shop: Shop) -> None:
    # Each eval scenario has its own database; here the seed is unique instead, because
    # proposal ids are a primary key in the shared test database.
    seed = f"proposal-{uuid.uuid4()}"
    with deterministic_ids(seed):
        pid = await _propose(shop)
    with deterministic_ids(seed):
        assert pid == new_id()


async def test_turn_id_comes_from_new_id(shop: Shop) -> None:
    ctx = turn_ctx(shop, shop.x)
    seed = f"turn-{uuid.uuid4()}"  # unique: message ids are a primary key across runs
    with deterministic_ids(seed):
        async with tenant_tx(ctx.tenant) as conn:
            conv = await conversations.create(conn, ctx)
            turn_id = await conversations.add_user_message(conn, ctx, conv["id"], "hello")
    with deterministic_ids(seed):
        assert turn_id == new_id()  # the first id handed out inside that scope


def test_budget_counts_on_the_engine_it_is_given() -> None:
    engine = create_async_engine("postgresql+asyncpg://u:p@main-db/app")
    s = get_settings().model_copy(update={"llm_provider": "gemini", "gemini_api_key": SecretStr("k")})
    router = get_provider(s, budget_engine=engine)
    assert router.budget.engine is engine
