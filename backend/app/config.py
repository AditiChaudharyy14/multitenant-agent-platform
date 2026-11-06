"""Application settings, read from the environment Docker Compose provides."""

from functools import lru_cache

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

    @property
    def allowed_origin_set(self) -> frozenset[str]:
        return frozenset(o.strip() for o in self.allowed_origins.split(",") if o.strip())


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
