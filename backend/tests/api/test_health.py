import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import main
from app.api import health as health_module
from app.db.session import app_engine
from app.domain.kb import embeddings
from app.main import app
from app.tenancy.orgs import OrgConfig


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_health_ok_when_database_reachable_and_model_loaded() -> None:
    await embeddings.warm_up()
    async with client() as c:
        response = await c.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "db": True, "embeddings": True}


async def test_health_503_until_the_embedding_model_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker's healthcheck must not call the backend healthy before the first
    search_knowledge can run without loading the model."""
    monkeypatch.setattr(embeddings, "_model", None)
    async with client() as c:
        response = await c.get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"ok": False, "db": True, "embeddings": False}


async def test_startup_loads_the_embedding_model(
    monkeypatch: pytest.MonkeyPatch, synced_orgs: dict[str, OrgConfig]
) -> None:
    """The lifespan loads the model and runs one embed before the app serves a request."""
    monkeypatch.setattr(embeddings, "_model", None)
    assert not embeddings.is_loaded()
    async with main.lifespan(app):
        assert embeddings.is_loaded()


async def test_health_503_when_database_unreachable(monkeypatch) -> None:
    # Nothing listens on port 1, so the connection is refused immediately.
    unreachable = create_async_engine("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    monkeypatch.setattr(health_module.db, "app_engine", unreachable)
    async with client() as c:
        response = await c.get("/api/health")
    assert response.status_code == 503
    assert response.json()["ok"] is False and response.json()["db"] is False


async def test_runtime_engine_connects_as_app_user() -> None:
    async with app_engine.connect() as connection:
        role = await connection.scalar(text("SELECT current_user"))
    assert role == "app_user"
