"""Structured logs: one JSON object per line, with request_id (also returned as the
X-Request-ID header), the route template and the org slug. Message text, emails and
passwords never appear in a log line.
"""

import io
import json
import logging
import re
import uuid
from collections.abc import Iterator

import httpx
import pytest

from app.agents.providers.fake import FakeProvider, tool_call
from app.api.chat import chat_limiter, get_chat_provider
from app.auth.ratelimit import login_limiter
from app.logging_setup import JsonFormatter, RedactFilter, request_context
from app.main import app
from tests.factories import Shop

pytestmark = pytest.mark.usefixtures("as_app_user")

ORIGIN = "http://localhost:3000"
SECRET_TEXT = "my private note about Aunt Kamala's birthday"
PASSWORD = "correct-horse-battery-9"


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    """Every record from every logger, through the app's own formatter and filter."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield buf
    root.removeHandler(handler)
    root.setLevel(old_level)


def lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                             headers={"Origin": ORIGIN}, timeout=30)  # fmt: skip


async def test_every_line_is_json_with_a_request_id_that_is_also_a_header(
    captured: io.StringIO, synced_orgs: dict
) -> None:
    async with client() as c:
        r = await c.get(f"/api/orgs/{synced_orgs['store'].slug}")
    assert r.status_code == 200
    rid = r.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", rid)
    records = lines(captured)
    assert records, "the request was not logged"
    mine = [x for x in records if x.get("request_id") == rid]
    access = next(x for x in mine if x["msg"] == "request")
    assert access["route"] == "/api/orgs/{slug}" and access["status"] == 200 and access["method"] == "GET"
    assert access["org"] == synced_orgs["store"].slug
    assert {"ts", "level", "logger", "msg"} <= set(access)


async def test_an_incoming_request_id_is_kept_only_if_it_is_safe(captured: io.StringIO) -> None:
    async with client() as c:
        kept = await c.get("/api/health", headers={"X-Request-ID": "abc-123-DEF-456"})
        replaced = await c.get("/api/health", headers={"X-Request-ID": "bad id\nwith newline" + "x" * 80})
    assert kept.headers["X-Request-ID"] == "abc-123-DEF-456"
    assert re.fullmatch(r"[0-9a-f]{32}", replaced.headers["X-Request-ID"])


async def test_message_text_emails_and_passwords_never_reach_the_logs(
    captured: io.StringIO, shop: Shop, synced_orgs: dict
) -> None:
    login_limiter.reset()
    chat_limiter.reset()
    slug = synced_orgs["store"].slug
    email = f"logcheck-{uuid.uuid4().hex[:8]}@example.test"
    fake = FakeProvider([tool_call("remember_preference", {"key": "note", "value": "oat milk"}), "Noted."])
    app.dependency_overrides[get_chat_provider] = lambda: fake
    try:
        async with client() as c:
            r = await c.post(f"/api/orgs/{slug}/auth/signup",
                             json={"email": email, "password": PASSWORD, "display_name": "Log Check"})  # fmt: skip
            assert r.status_code == 201, r.text
            bad = await c.post(f"/api/orgs/{slug}/auth/login", json={"email": email, "password": "wrong-" + PASSWORD})
            assert bad.status_code == 401
            cid = (await c.post("/api/conversations")).json()["id"]
            r = await c.post(f"/api/conversations/{cid}/messages", json={"content": SECRET_TEXT})
            assert r.status_code == 200
    finally:
        app.dependency_overrides.pop(get_chat_provider, None)
    text = captured.getvalue()
    assert text, "nothing was logged"
    for forbidden in (SECRET_TEXT, "Aunt Kamala", email, PASSWORD, "oat milk"):
        assert forbidden not in text, forbidden
    assert any(x.get("route") == "/api/conversations/{conversation_id}/messages" for x in lines(captured))


def test_redact_filter_masks_emails_and_secrets_in_any_record(captured: io.StringIO) -> None:
    log = logging.getLogger("app.test")
    log.warning("login failed for %s from %s", "someone@example.test", "10.0.0.1")
    log.error("db url postgresql+asyncpg://app_user:hunter2secret@db:5432/app")
    out = captured.getvalue()
    assert "someone@example.test" not in out and "hunter2secret" not in out
    assert "***@example.test" in out or "[email]" in out


def test_request_context_is_empty_outside_a_request() -> None:
    assert request_context() == {}
