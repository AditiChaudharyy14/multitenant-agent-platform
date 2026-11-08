"""Every API response carries the security headers (JSON, errors and the SSE stream)."""

import httpx
import pytest

from app.main import app

EXPECTED = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}


@pytest.mark.parametrize(("method", "path"), [("GET", "/api/health"), ("GET", "/api/orgs/no-such-org"),
                                              ("GET", "/api/me"), ("POST", "/api/auth/logout")])  # fmt: skip
async def test_api_responses_carry_security_headers(method: str, path: str) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                 headers={"Origin": "http://evil.example"}) as c:  # fmt: skip
        r = await c.request(method, path)  # includes a 404, a 401 and a CSRF 403
    for name, value in EXPECTED.items():
        assert r.headers.get(name) == value, (path, name, r.headers.get(name))
    assert "camera=()" in r.headers["permissions-policy"]
