"""Application settings, read from the environment Docker Compose provides."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # All required, so a missing URL fails at startup rather than on first use.
    database_url_owner: str  # app_owner: migrations and seed
    database_url_app: str  # app_user: runtime requests
    database_url_readonly: str  # app_readonly: admin analytics


@lru_cache
def get_settings() -> Settings:
    return Settings()
