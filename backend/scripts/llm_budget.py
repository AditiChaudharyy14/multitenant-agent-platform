"""`make llm-budget`: today's LLM requests per model against the daily budget."""

import asyncio

from app.agents.providers.budget import DailyBudget, next_reset, quota_day
from app.config import get_settings
from app.db.session import app_engine


async def main() -> None:
    s = get_settings()
    print(f"quota day {quota_day()} (Pacific); resets {next_reset():%Y-%m-%d %H:%M} UTC")
    for provider in ("gemini", "mistral"):
        budget = DailyBudget(provider, getattr(s, f"llm_daily_budget_{provider}"))
        for model in s.llm_models(provider):
            used = await budget.used(model)
            active = " (active provider)" if provider == s.llm_provider else ""
            print(f"  {provider}/{model}: {used}/{budget.limit} used, {budget.limit - used} left{active}")
    await app_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
