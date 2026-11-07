"""Application settings, read from the environment Docker Compose provides."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Required, so a missing URL fails at startup rather than on first use.
    database_url_app: str  # app_user: runtime requests
    database_url_readonly: str  # app_readonly: admin analytics
    # app_owner: migrations, org sync, seed and tests only. The running backend service
    # does not get it; those commands run in the one-shot `ops` service (see Makefile).
    database_url_owner: str | None = None

    seed_dir: str = "/seed"  # holds <slug>/org.yaml, data.yaml and kb/
    embed_cache_dir: str = "/models"  # fastembed model cache (a named Docker volume)
    # Optional fixed "now" for seeding: ISO datetime, naive = org timezone. Empty = real clock.
    seed_now: str | None = None
    # Comma-separated list of origins allowed to send non-GET requests to /api (CSRF).
    allowed_origins: str = "http://localhost:3000"
    # "local" turns off the Secure flag on the session cookie (plain-HTTP dev only).
    app_env: str = "local"

    # ── LLM ── Gemini is the default: Mistral's free plan isn't available to this account.
    # "fake" is the scripted offline provider every test uses.
    llm_provider: Literal["gemini", "mistral", "fake"] = "gemini"
    gemini_api_key: SecretStr = SecretStr("")
    llm_model_gemini: str = "gemini-3.5-flash-lite"
    llm_fallback_models_gemini: str = "gemini-3.1-flash-lite"  # comma-separated, tried in order
    llm_rpm_gemini: int = 12  # per model; the free tier allows 15
    llm_daily_budget_gemini: int = 450  # per model; the free tier allows 500
    mistral_api_key: SecretStr = SecretStr("")
    llm_model_mistral: str = "mistral-small-latest"
    llm_fallback_models_mistral: str = ""
    llm_rpm_mistral: int = 60
    llm_daily_budget_mistral: int = 1000
    # off: live provider. record: live, and save responses. replay: saved responses only.
    eval_mode: Literal["off", "record", "replay"] = "off"
    eval_cassette_dir: str = "/eval/cassettes"
    # With LLM_PROVIDER=fake: a YAML scenario that scripts the "model" (UI end-to-end tests).
    # Empty = the plain FakeProvider that unit tests script themselves.
    fake_scenario_file: str = ""

    # ── Email ── Transactional outbox + background sender (app/domain/notifications).
    # Development delivers to Mailpit (web UI on :8025); nothing ever reaches a real inbox.
    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    email_from: str = "no-reply@agents.local"  # shown as "<Org name> <email_from>"
    # Off unless the backend service turns it on (compose does), so tests and evals never
    # start background loops; tests call the run_once() functions directly.
    email_worker_enabled: bool = False
    email_poll_seconds: int = 10  # how often the sender looks for pending mail
    reminder_interval_seconds: int = 300  # how often the 24 h booking-reminder scan runs

    def llm_models(self, provider: str) -> list[str]:
        """Primary model first, then the fallbacks in order."""
        primary = getattr(self, f"llm_model_{provider}")
        fallbacks = getattr(self, f"llm_fallback_models_{provider}")
        return [primary, *(m.strip() for m in fallbacks.split(",") if m.strip())]

    @property
    def allowed_origin_set(self) -> frozenset[str]:
        return frozenset(o.strip() for o in self.allowed_origins.split(",") if o.strip())


# Admin analytics (ai_usage): USD per 1M tokens, (input, output), per model. These are
# ESTIMATES at paid-tier list prices; this deployment runs on free tiers, so the actual
# cost is $0. A model missing here is reported as unpriced, never as free.
LLM_PRICING_NOTE = "estimated at paid-tier prices; free tier actual cost $0"
LLM_PRICES_USD_PER_1M: dict[str, tuple[str, str]] = {
    "gemini-3.5-flash-lite": ("0.10", "0.40"),
    "gemini-3.1-flash-lite": ("0.10", "0.40"),
    "mistral-small-latest": ("0.10", "0.30"),
    "fake-model": ("0", "0"),  # the offline scripted provider used by tests and e2e
}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def require_owner_url() -> str:
    """The app_owner URL, for owner-only commands. The running app never has it."""
    url = get_settings().database_url_owner
    if not url:
        raise RuntimeError(
            "DATABASE_URL_OWNER is not set: run owner commands through the ops service "
            "(make migrate / make sync-orgs / make seed / make test)"
        )
    return url
