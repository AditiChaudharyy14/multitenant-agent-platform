import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.api import health as health_module
from app.db.session import app_engine
from app.main import app


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_health_ok_when_database_reachable() -> None:
    async with client() as c:
        response = await c.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "db": True}


async def test_health_503_when_database_unreachable(monkeypatch) -> None:
    # Nothing listens on port 1, so the connection is refused immediately.
    unreachable = create_async_engine("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    monkeypatch.setattr(health_module.db, "app_engine", unreachable)
    async with client() as c:
        response = await c.get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"ok": False, "db": False}


async def test_runtime_engine_connects_as_app_user() -> None:
    async with app_engine.connect() as connection:
        role = await connection.scalar(text("SELECT current_user"))
    assert role == "app_user"
