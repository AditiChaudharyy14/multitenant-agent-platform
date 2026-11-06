from collections.abc import AsyncIterator

import pytest

from app.db.engine import app_engine


@pytest.fixture(autouse=True)
async def dispose_engine() -> AsyncIterator[None]:
    """Close pooled connections after each test, as each test gets its own event loop."""
    yield
    await app_engine.dispose()
