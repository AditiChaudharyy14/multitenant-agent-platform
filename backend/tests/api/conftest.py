"""Fixtures shared by the API tests."""

from collections.abc import Iterator

import pytest

from app.agents.providers.fake import FakeProvider
from app.api.chat import chat_limiter, get_chat_provider
from app.main import app


@pytest.fixture(autouse=True)
def _reset() -> None:
    """The chat limiter is per process; ids repeat across tests, so start every test fresh."""
    chat_limiter.reset()


@pytest.fixture
def fake() -> Iterator[FakeProvider]:
    """A scripted model behind the chat API, for this test only."""
    provider = FakeProvider()
    app.dependency_overrides[get_chat_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_chat_provider, None)
