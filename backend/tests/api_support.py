"""A logged-in HTTP client for API tests: a real session row, the real app, no network."""

import uuid

import httpx

from app.auth.sessions import COOKIE_NAME, create_session
from app.db.session import app_engine
from app.main import app

ORIGIN = "http://localhost:3000"


async def client_for(org_id: uuid.UUID, user: uuid.UUID, role: str = "customer") -> httpx.AsyncClient:
    async with app_engine.begin() as conn:
        token = await create_session(conn, org_id, user, role)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                             headers={"Origin": ORIGIN}, cookies={COOKIE_NAME: token}, timeout=30)  # fmt: skip


def anonymous() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                             headers={"Origin": ORIGIN}, timeout=30)  # fmt: skip
