"""Auth isolation: identity comes only from the server-side session.

Runs the real app over ASGI against app_test, with the runtime engine connected as
app_user. Orgs are picked by vertical (via the synced seed configs), never by slug literal.
"""

import hashlib
import uuid
from collections.abc import Callable
from datetime import timedelta

import httpx
import pytest
from argon2 import PasswordHasher
from sqlalchemy import text

from app import clock
from app.auth.ratelimit import login_limiter
from app.db.session import TenantContext, app_engine, tenant_tx
from app.main import app
from app.tenancy.orgs import OrgConfig

pytestmark = pytest.mark.usefixtures("as_app_user")

ORIGIN = "http://localhost:3000"  # in ALLOWED_ORIGINS (default and .env.example)
PASSWORD = "correct horse battery"
INVALID_LOGIN = {"detail": "Invalid email or password"}

Client = Callable[..., httpx.AsyncClient]


@pytest.fixture(autouse=True)
def _reset_login_limiter() -> None:
    login_limiter.reset()


@pytest.fixture
def client() -> Client:
    def make(sid: str | None = None, origin: str | None = ORIGIN) -> httpx.AsyncClient:
        headers = {"Origin": origin} if origin else {}
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", headers=headers)
        if sid is not None:
            c.cookies.set("sid", sid)
        return c

    return make


@pytest.fixture
def store(synced_orgs: dict[str, OrgConfig]) -> str:
    return synced_orgs["store"].slug


@pytest.fixture
def booking(synced_orgs: dict[str, OrgConfig]) -> str:
    return synced_orgs["booking"].slug


@pytest.fixture
def email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.test"


async def signup(c: httpx.AsyncClient, slug: str, email: str, **extra) -> httpx.Response:
    body = {"email": email, "password": PASSWORD, "display_name": "Asha", **extra}
    return await c.post(f"/api/orgs/{slug}/auth/signup", json=body)


async def login(c: httpx.AsyncClient, slug: str, email: str, password: str = PASSWORD):
    return await c.post(f"/api/orgs/{slug}/auth/login", json={"email": email, "password": password})


async def sid_after_signup(client: Client, slug: str, email: str, **extra) -> str:
    async with client() as c:
        r = await signup(c, slug, email, **extra)
    assert r.status_code == 201, r.text
    return r.cookies["sid"]


async def me(client: Client, sid: str) -> httpx.Response:
    async with client(sid) as c:
        return await c.get("/api/me")


async def org_id_of(slug: str) -> uuid.UUID:
    async with app_engine.connect() as conn:
        return await conn.scalar(text("SELECT id FROM orgs WHERE slug = :s"), {"s": slug})


# ───────────── Cross-org login ─────────────


async def test_login_at_other_org_is_same_401_as_wrong_password(
    client: Client, store: str, booking: str, email: str
) -> None:
    await sid_after_signup(client, store, email)
    async with client() as c:
        other_org = await login(c, booking, email)
        wrong_pw = await login(c, store, email, "wrong password!!")
        unknown = await login(c, store, f"nobody-{email}")
        ok = await login(c, store, email)
    assert other_org.status_code == wrong_pw.status_code == unknown.status_code == 401
    assert other_org.json() == wrong_pw.json() == unknown.json() == INVALID_LOGIN
    assert other_org.content == wrong_pw.content == unknown.content
    assert "sid" not in other_org.cookies
    assert ok.status_code == 200


async def test_same_email_registers_independently_in_both_orgs(
    client: Client, store: str, booking: str, email: str
) -> None:
    sid_a = await sid_after_signup(client, store, email)
    sid_b = await sid_after_signup(client, booking, email)
    me_a, me_b = (await me(client, sid_a)).json(), (await me(client, sid_b)).json()
    assert me_a["user"]["id"] != me_b["user"]["id"]
    assert (me_a["org"]["slug"], me_b["org"]["slug"]) == (store, booking)
    assert me_a["org"]["vertical"] == "store" and me_b["org"]["vertical"] == "booking"
    # Each login only works in its own org and lands in that org.
    async with client() as c:
        r = await login(c, booking, email)
    assert r.status_code == 200
    assert (await me(client, r.cookies["sid"])).json()["user"]["id"] == me_b["user"]["id"]


async def test_unknown_slug_is_404(client: Client, email: str) -> None:
    async with client() as c:
        r = await login(c, "no-such-org", email)
    assert r.status_code == 404


# ───────────── Identity ignores request-supplied ids ─────────────


async def test_request_supplied_org_and_user_ids_are_ignored(
    client: Client, store: str, booking: str, email: str
) -> None:
    sid_a = await sid_after_signup(client, store, email)
    sid_b = await sid_after_signup(client, booking, f"b-{email}")
    other_org = str(await org_id_of(booking))
    other_user = (await me(client, sid_b)).json()["user"]["id"]
    baseline = (await me(client, sid_a)).json()

    async with client(sid_a) as c:
        via_header = await c.get("/api/me", headers={"X-Org-Id": other_org, "X-User-Id": other_user, "X-Role": "admin"})
        via_query = await c.get("/api/me", params={"org_id": other_org, "user_id": other_user, "role": "admin"})
        via_body = await c.request("GET", "/api/me", json={"org_id": other_org, "user_id": other_user, "role": "admin"})
    for r in (via_header, via_query, via_body):
        assert r.status_code == 200
        assert r.json() == baseline
    assert baseline["org"]["slug"] == store and baseline["user"]["role"] == "customer"


async def test_no_cookie_is_401_even_with_identity_headers(client: Client, booking: str) -> None:
    async with client() as c:
        r = await c.get(
            "/api/me",
            headers={"X-Org-Id": str(await org_id_of(booking)), "X-User-Id": str(uuid.uuid4())},
            params={"org_id": str(await org_id_of(booking))},
        )
    assert r.status_code == 401


# ───────────── Cookie tampering, expiry, logout ─────────────


async def test_tampered_cookie_is_401(client: Client, store: str, email: str) -> None:
    sid = await sid_after_signup(client, store, email)
    flipped = sid[:-1] + ("A" if sid[-1] != "A" else "B")
    for bad in (flipped, "x" * len(sid), "", "not-a-token"):
        r = await me(client, bad)
        assert r.status_code == 401, bad
        assert r.json() == {"detail": "Not authenticated"}
    assert (await me(client, sid)).status_code == 200  # control


async def test_expired_session_is_401(client: Client, store: str, email: str) -> None:
    sid = await sid_after_signup(client, store, email)
    with clock.freeze(clock.now() + timedelta(days=6, hours=23)):
        assert (await me(client, sid)).status_code == 200
    with clock.freeze(clock.now() + timedelta(days=8)):
        assert (await me(client, sid)).status_code == 401


async def test_logout_makes_old_cookie_useless(client: Client, store: str, email: str) -> None:
    sid = await sid_after_signup(client, store, email)
    async with client(sid) as c:
        r = await c.post("/api/auth/logout")
    assert r.status_code == 204
    cleared = r.headers["set-cookie"]
    assert cleared.startswith("sid=") and ("Max-Age=0" in cleared or "expires=" in cleared.lower())
    assert (await me(client, sid)).status_code == 401
    async with app_engine.connect() as conn:
        n = await conn.scalar(
            text("SELECT count(*) FROM sessions WHERE token_hash = :h"),
            {"h": hashlib.sha256(sid.encode()).digest()},
        )
    assert n == 0


async def test_every_login_issues_a_new_token(client: Client, store: str, email: str) -> None:
    first = await sid_after_signup(client, store, email)
    async with client() as c:
        second = (await login(c, store, email)).cookies["sid"]
        third = (await login(c, store, email)).cookies["sid"]
    assert len({first, second, third}) == 3


async def test_session_cookie_flags(client: Client, store: str, email: str) -> None:
    async with client() as c:
        r = await signup(c, store, email)
    cookie = r.headers["set-cookie"]
    parts = {p.strip().split("=")[0].lower(): p.strip() for p in cookie.split(";")}
    assert "httponly" in parts
    assert parts["samesite"].lower() == "samesite=lax"
    assert parts["path"] == "Path=/"
    assert parts["max-age"] == f"Max-Age={7 * 24 * 3600}"
    assert "secure" not in parts  # APP_ENV=local in tests


# ───────────── Storage: argon2id hashes, no plaintext ─────────────


async def test_passwords_and_tokens_are_never_stored_in_plaintext(client: Client, store: str, email: str) -> None:
    sid = await sid_after_signup(client, store, email)
    user_id = uuid.UUID((await me(client, sid)).json()["user"]["id"])
    org_id = await org_id_of(store)
    async with tenant_tx(TenantContext(org_id, None, "admin")) as conn:
        pw_hash = await conn.scalar(text("SELECT password_hash FROM users WHERE id = :u"), {"u": user_id})
        user_row = await conn.scalar(text("SELECT u::text FROM users u WHERE id = :u"), {"u": user_id})
        session_rows = list(
            await conn.scalars(text("SELECT s::text FROM sessions s WHERE user_id = :u"), {"u": user_id})
        )
        token_hash = await conn.scalar(text("SELECT token_hash FROM sessions WHERE user_id = :u"), {"u": user_id})
    assert pw_hash.startswith("$argon2id$")
    assert PASSWORD not in user_row
    assert session_rows and all(PASSWORD not in r and sid not in r for r in session_rows)
    assert bytes(token_hash) == hashlib.sha256(sid.encode()).digest()


async def test_stale_hash_is_rehashed_on_login(client: Client, store: str, email: str) -> None:
    sid = await sid_after_signup(client, store, email)
    user_id = uuid.UUID((await me(client, sid)).json()["user"]["id"])
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    ctx = TenantContext(await org_id_of(store), None, "admin")
    async with tenant_tx(ctx) as conn:
        await conn.execute(text("UPDATE users SET password_hash = :h WHERE id = :u"), {"h": weak, "u": user_id})
    async with client() as c:
        assert (await login(c, store, email)).status_code == 200
    async with tenant_tx(ctx) as conn:
        new = await conn.scalar(text("SELECT password_hash FROM users WHERE id = :u"), {"u": user_id})
    assert new != weak and new.startswith("$argon2id$")
    assert not PasswordHasher().check_needs_rehash(new)


# ───────────── CSRF ─────────────


async def test_foreign_origin_post_is_403(client: Client, store: str, email: str) -> None:
    for origin in ("https://evil.example", None):
        async with client(origin=origin) as c:
            r = await signup(c, store, email)
            assert r.status_code == 403, origin
            assert r.json() == {"detail": "Forbidden origin"}
            r = await login(c, store, email)
            assert r.status_code == 403
    # Nothing was created: signing up from the real origin now succeeds.
    await sid_after_signup(client, store, email)


async def test_foreign_origin_cannot_log_out(client: Client, store: str, email: str) -> None:
    sid = await sid_after_signup(client, store, email)
    async with client(sid, origin="https://evil.example") as c:
        assert (await c.post("/api/auth/logout")).status_code == 403
    assert (await me(client, sid)).status_code == 200


# ───────────── Signup can only create customers ─────────────


async def test_signup_ignores_role_and_org_in_body(client: Client, store: str, booking: str, email: str) -> None:
    sid = await sid_after_signup(
        client, store, email, role="admin", org_id=str(await org_id_of(booking)), user_id=str(uuid.uuid4())
    )
    body = (await me(client, sid)).json()
    assert body["user"]["role"] == "customer"
    assert body["org"]["slug"] == store


async def test_signup_validation(client: Client, store: str, email: str) -> None:
    async with client() as c:
        short = await c.post(
            f"/api/orgs/{store}/auth/signup",
            json={"email": email, "password": "123456789", "display_name": "A"},
        )
        bad_email = await signup(c, store, "not-an-email")
        assert (await signup(c, store, email)).status_code == 201
        dup = await signup(c, store, email.upper())  # citext: same email
    assert short.status_code == 422
    assert bad_email.status_code == 422
    assert dup.status_code == 409


# ───────────── Login rate limit ─────────────


async def test_login_rate_limited_per_ip_and_email(client: Client, store: str, email: str) -> None:
    async with client() as c:
        for _ in range(10):
            assert (await login(c, store, email, "wrong password!!")).status_code == 401
        limited = await login(c, store, email, "wrong password!!")
        other_email = await login(c, store, f"other-{email}", "wrong password!!")
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert other_email.status_code == 401  # keyed per email, not just per IP
    with clock.freeze(clock.now() + timedelta(seconds=61)):
        async with client() as c:
            assert (await login(c, store, email, "wrong password!!")).status_code == 401


# ───────────── Public org branding ─────────────


async def test_public_org_endpoint(client: Client, synced_orgs: dict[str, OrgConfig]) -> None:
    cfg = synced_orgs["booking"]
    async with client() as c:
        r = await c.get(f"/api/orgs/{cfg.slug}")
        missing = await c.get("/api/orgs/no-such-org")
    assert r.status_code == 200
    assert r.json() == {
        "slug": cfg.slug, "name": cfg.name, "vertical": cfg.vertical,
        "branding": cfg.branding.model_dump(), "features": cfg.features.model_dump(),
    }  # fmt: skip
    assert "id" not in r.json()
    assert missing.status_code == 404
