"""Live smoke tests: one real request per provider. Run with `make test-live`, never by
`make test` (which runs in the ops container with no keys and a FakeProvider).

Each run spends about one request of the daily budget, counted in the dev database.
"""

import pytest

from app.agents.providers import get_provider
from app.agents.providers.base import ChatRequest, Finished, Message, TextDelta
from app.config import get_settings
from app.db.session import app_engine


@pytest.mark.parametrize("name", ["gemini", "mistral"])
async def test_live_provider_answers(name: str) -> None:
    s = get_settings()
    if not getattr(s, f"{name}_api_key").get_secret_value():
        pytest.skip(f"{name.upper()}_API_KEY not set")
    provider = get_provider(s.model_copy(update={"llm_provider": name, "eval_mode": "off"}))
    req = ChatRequest(system="", messages=[Message("user", "Reply with the single word: ok")], max_output_tokens=50)
    try:
        events = [e async for e in provider.stream(req)]
    finally:
        await app_engine.dispose()
    assert isinstance(events[-1], Finished)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)).strip()
