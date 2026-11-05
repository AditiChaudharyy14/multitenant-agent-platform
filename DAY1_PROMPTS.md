# Build plan + Day 1 prompts (Thu 5 Nov 2026)

## Schedule (deadline Mon 9 Nov 16:00 NPT; Tue 10 Nov is emergency-only grace)

| Day | Goal | Done when |
|---|---|---|
| **Thu 5 (tonight)** | Scaffold, schema + RLS, auth, first isolation tests | `make up && make migrate && make test` green; RLS proven as `app_user` |
| **Fri 6** | Seed both orgs + `make seed`, KB ingestion + hybrid search, agent loop, providers, SSE chat endpoint | Chat answers a KB question with a citation over SSE |
| **Sat 7** | Agent A tools + proposals; Agent B slot engine + tools; concurrency test | Order placed/edited/tracked/reordered and booking made/rescheduled via API |
| **Sun 8** | Frontend: branding, auth pages, chat, streaming, Stop, tool activity, cards, sidebar, My orders/bookings panel, 375 px | Full demo flow works in the browser |
| **Mon 9 AM** | `make eval` (≥15 scenarios per agent) → fix → admin agent if evals are green | `eval/results.md` generated |
| **Mon 9 PM** | README, record demo (5–8 min), submit by 16:00 | Email sent |

Rule: if isolation tests aren't green, nothing new gets built.

---

## Prompt 0: setup (run in your terminal first, not in Claude Code)

```bash
mkdir multitenant-agents && cd multitenant-agents && git init
# put CLAUDE.md and schema_reference.sql (from this chat) in the repo root
git clone --depth 1 https://github.com/anthropics/skills /tmp/anthropic-skills
mkdir -p .claude/skills
cp -r /tmp/anthropic-skills/skills/frontend-design /tmp/anthropic-skills/skills/webapp-testing .claude/skills/ 2>/dev/null || ls /tmp/anthropic-skills/skills
git add . && git commit -m "chore: project rules and schema reference"
claude
```

If the `cp` prints a folder list instead, the skills moved; copy those two folders by their new path.

---

## Prompt 1: scaffold

```
Read CLAUDE.md fully. Enter plan mode and show me the plan before writing code.

Scaffold the monorepo exactly as in CLAUDE.md "Repo layout":
- docker-compose.yml with services: db (pgvector/pgvector:pg16), backend (FastAPI, uvicorn --reload), frontend (Next.js dev). Healthchecks on db and backend.
- docker/db/init.sql creating roles app_owner, app_user (NOBYPASSRLS), app_readonly (NOBYPASSRLS), rls_definer (NOLOGIN BYPASSRLS), database owned by app_owner. Passwords come from env.
- backend: pyproject (fastapi, uvicorn, sqlalchemy[asyncio], asyncpg, alembic, pydantic-settings, argon2-cffi, pytest, pytest-asyncio, httpx). app/config.py with pydantic-settings; app/clock.py with an injectable now() (freezable in tests). GET /api/health returns {"ok": true, "db": true} after a SELECT 1.
- Two DB URLs in config: DATABASE_URL_OWNER (migrations/seed) and DATABASE_URL_APP (runtime, app_user). Plus DATABASE_URL_READONLY.
- frontend: Next.js App Router + TypeScript + Tailwind; a placeholder page at /a/[slug].
- Makefile targets: up, down, migrate, seed (stub), test, eval (stub), advance (stub), logs.
- .env.example with every variable, safe placeholder values, comments. .gitignore includes .env.

Done when: `make up` starts all three services, /api/health returns ok, the frontend page loads. Commit.
```

---

## Prompt 2: schema + RLS migration

```
Read CLAUDE.md and schema_reference.sql. Plan mode first.

Create the first Alembic migration from schema_reference.sql using raw op.execute() SQL. Keep every constraint, policy, grant and the busy_intervals() SECURITY DEFINER function. Alembic runs as app_owner.

Then write backend/app/db/session.py:
- an async engine for app_user and one for app_readonly.
- `tenant_tx(ctx)`: async context manager that opens ONE transaction and immediately runs
  SELECT set_config('app.org_id',$1,true), set_config('app.user_id',$2,true), set_config('app.role',$3,true).
  Never plain SET. The read-only variant also runs SET TRANSACTION READ ONLY first.
- `anon_tx(org_id)` for login/signup: sets only app.org_id.

Tests FIRST in backend/tests/isolation/test_rls.py, connecting as app_user (assert current_user = 'app_user'):
1. With no settings, SELECT from every tenant table returns 0 rows (insert fixture rows as app_owner with app.org_id set, because FORCE RLS applies to the owner too).
2. With org A set, org B rows are invisible in every tenant table.
3. INSERT with org_id of org B while app.org_id = A fails (WITH CHECK).
4. Customer role: user X cannot see user Y's orders, bookings, conversations, messages, proposals in the same org; admin role in the same org can.
5. busy_intervals() returns only the current org's confirmed bookings and has no user_id column; returns nothing when app.org_id is unset.
6. Exclusion constraint: two overlapping confirmed bookings for the same practitioner → second fails with exclusion_violation; back-to-back ('[)') succeeds; a cancelled booking doesn't block.
7. app_readonly cannot INSERT/UPDATE/DELETE anything.

Done when `make migrate && make test` is green. Commit.
```

---

## Prompt 3: org config + auth

```
Read CLAUDE.md. Plan mode first.

1. seed/himali-tea/org.yaml and seed/lotus-wellness/org.yaml: slug, name, vertical, timezone, branding (primary/secondary colours, logo emoji, agent name, greeting). app/tenancy/orgs.py loads them; nothing in code may branch on a slug.

2. Auth (app/auth/), no third-party provider:
- argon2id via argon2-cffi (PasswordHasher defaults), with rehash-on-login if params change.
- POST /api/orgs/{slug}/auth/signup {email, password, display_name} → role customer, org fixed by slug. Password ≥ 10 chars.
- POST /api/orgs/{slug}/auth/login → looks the user up inside anon_tx(org_of_slug). Wrong org, unknown email, or wrong password all return the SAME 401 body and similar timing (always run a dummy argon2 verify on miss).
- Session: secrets.token_urlsafe(32) in cookie `sid` (HttpOnly, SameSite=Lax, Secure when not local, Path=/, 7-day expiry); only sha256(token) stored in sessions with org_id, user_id, role. New token on every login. POST /api/auth/logout deletes the row and clears the cookie.
- GET /api/me → {user, org: {slug, name, vertical, branding}}.
- FastAPI dependency `current_ctx` → ToolContext from the session ONLY. It must ignore any org_id/user_id in headers, query or body.
- CSRF: for every non-GET under /api, reject unless the Origin header matches ALLOWED_ORIGINS.
- Rate limit on login: 10 attempts/min per IP+email.

3. Tests FIRST in backend/tests/isolation/test_auth.py:
- sign up at himali-tea, then login at lotus-wellness with the same credentials → 401.
- the same email can register independently in both orgs; they are different users.
- requests with an `X-Org-Id` header, `?org_id=` or a body org_id pointing at the other org don't change /api/me's org.
- a tampered or expired cookie → 401; logout makes the old cookie useless.
- password hashes in the DB start with $argon2id$.
- a POST with a foreign Origin → 403.

Done when `make test` is green. Commit. Append to docs/ai-usage.md.
```

---

## Before you sleep (5 min)

- Read `app/db/session.py` and the RLS migration yourself, line by line. These are the two files reviewers will attack and ask you about.
- `git log` should show 3–4 clean commits.
