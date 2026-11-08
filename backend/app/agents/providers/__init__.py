"""LLM providers. get_provider() builds the one selected by LLM_PROVIDER (default gemini).

    gemini / mistral → ModelRouter(adapter, [primary, *fallbacks], daily budget, RPM limiters)
    fake             → FakeProvider (what every `make test` test runs on), or, with
                       FAKE_SCENARIO_FILE, a ScriptedProvider (Playwright, no quota spent)
    EVAL_MODE=record|replay wraps the result in RecordReplayProvider; replay needs no key.

Behaviour is keyed on the provider name and settings only; nothing here knows an org.
"""

from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.providers.base import LLMProvider, ProviderAuthError
from app.agents.providers.budget import DailyBudget
from app.agents.providers.fake import FakeProvider
from app.agents.providers.limiter import limiter_for
from app.agents.providers.recording import RecordReplayProvider
from app.agents.providers.router import ModelAdapter, ModelRouter
from app.agents.providers.scripted import ScriptedProvider, load_scenario
from app.config import Settings, get_settings


def _live(settings: Settings, name: str, budget_engine: AsyncEngine | None = None) -> LLMProvider:
    key = getattr(settings, f"{name}_api_key").get_secret_value()
    if not key:
        raise ProviderAuthError(
            f"{name.upper()}_API_KEY is not set: add it to .env, choose another LLM_PROVIDER, "
            f"or replay recorded responses with EVAL_MODE=replay"
        )
    adapter: ModelAdapter
    if name == "gemini":
        from app.agents.providers.gemini import GeminiAdapter

        adapter = GeminiAdapter(key)
    else:
        from app.agents.providers.mistral import MistralAdapter

        adapter = MistralAdapter(key)
    models = settings.llm_models(name)
    rpm = getattr(settings, f"llm_rpm_{name}")
    budget = DailyBudget(name, getattr(settings, f"llm_daily_budget_{name}"), budget_engine)
    return ModelRouter(adapter, models, budget, {m: limiter_for(name, m, rpm) for m in models})


def get_provider(settings: Settings | None = None, *, budget_engine: AsyncEngine | None = None) -> LLMProvider:
    """budget_engine: where llm_usage is counted. Default: the app engine. The eval runner
    passes the main database's engine, because it points app_engine at per-scenario clones."""
    s = settings or get_settings()
    if s.eval_mode == "replay":
        return RecordReplayProvider("replay", s.eval_cassette_dir)  # offline: no key, no budget
    if s.llm_provider == "fake":
        inner: LLMProvider = (
            ScriptedProvider(load_scenario(s.fake_scenario_file)) if s.fake_scenario_file else FakeProvider()
        )
    else:
        inner = _live(s, s.llm_provider, budget_engine)
    if s.eval_mode == "record":
        return RecordReplayProvider("record", s.eval_cassette_dir, inner)
    return inner
