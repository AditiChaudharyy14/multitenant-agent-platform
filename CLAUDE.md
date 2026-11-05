# CLAUDE.md — Multi-Tenant Agent Platform (take-home)

Read this file fully before every task. The invariants below are non-negotiable. If a request conflicts with them, stop and say so.

## Project

One chat platform hosting two AI agents for two fictional organisations:

- **Himali Tea House** (`himali-tea`, vertical `store`): an ordering agent covering product Q&A and the order life cycle (create, edit, track, cancel, reorder).
- **Lotus Wellness Studio** (`lotus-wellness`, vertical `booking`): a booking agent covering service Q&A and appointments (book, reschedule, cancel) with no conflicts.
- **Stretch:** a read-only admin analytics agent per org.

Deadline: submit by **Mon 9 Nov 2026, 16:00 NPT** (grace: end of Tue 10 Nov).

How the work is scored:

| Area | Weight |
|---|---|
| Agent engineering | 20% |
| Tenant isolation | 20% |
| Business rules | 15% |
| Evals | 15% |
| Retrieval | 10% |
| UX | 10% |
| Code and communication | 10% |
| Admin agent | bonus |

Correctness enforced below the LLM is the whole strategy.

## Stack

- **Environment:** develop inside WSL2 Ubuntu, with the repo in the Linux home directory (never under /mnt/c). The backend container uses `python:3.12-slim`; the host Python version is irrelevant. All text files use LF line endings.
- **Backend:** FastAPI (Python 3.12), SQLAlchemy 2 async + asyncpg, Alembic (raw SQL migrations), Pydantic v2, argon2-cffi.
- **Database:** Postgres 16 with `pgvector`, `btree_gist` and `citext` (Docker image `pgvector/pgvector:pg16`).
- **Embeddings:** local `fastembed` with `BAAI/bge-small-en-v1.5` (384 dimensions). No API key needed, so seeding is deterministic.
- **LLM:** a provider interface with `mistral` and `gemini` adapters, selected by the `LLM_PROVIDER` env var. Default `mistral`, because the free tiers are tight.
- **Frontend:** Next.js (App Router) + TypeScript + Tailwind, react-markdown + rehype-highlight.
- **Streaming:** SSE over a POST fetch stream. Stop works through AbortController plus server-side cancellation.
- **Run:** `docker compose` + Makefile.

## Repo layout

```
backend/app/
  main.py, config.py, clock.py
  db/            engine, tenant_session(), roles
  auth/          passwords, sessions, routes
  tenancy/       org config loader (seed/<slug>/org.yaml), ToolContext
  agents/        loop.py (<200 lines), prompts/, providers/{base,mistral,gemini}.py
  agents/tools/  store/, booking/, admin/, common/ (search_knowledge, confirm_proposal)
  domain/        orders/, bookings/, slots/, kb/, proposals/, analytics/
  api/           chat (SSE), conversations, orders, bookings, admin
backend/migrations/
backend/tests/   isolation/, concurrency/, domain/, api/
frontend/        app/a/[slug]/{page,login,signup,chat,admin}
seed/<slug>/     org.yaml, data.yaml, kb/*.md
eval/            scenarios/{store,booking,admin}/*.yaml, runner.py, results.md
docs/ai-usage.md
Makefile, docker-compose.yml, .env.example
```

## NON-NEGOTIABLE INVARIANTS

1. **Identity comes only from the server-side session.** `org_id`, `user_id` and `role` are never read from URL params, headers or request bodies. The URL slug is used only for branding and for choosing the org at sign-up or login.
2. **Every tenant table has `org_id`, with `ENABLE` and `FORCE ROW LEVEL SECURITY`.** The tenant policy compares `org_id` to `NULLIF(current_setting('app.org_id', true), '')::uuid`. Customer-owned tables (orders, order_items, order_status_history, bookings, conversations, messages, proposals) also carry `user_id` and a **RESTRICTIVE** policy: `app.role = 'admin' OR user_id = app.user_id`.
3. **Three database roles.**
   - `app_owner` runs migrations.
   - `app_user` is used at runtime: not an owner, with NOBYPASSRLS.
   - `app_readonly` is used by admin analytics: SELECT only, and every transaction is `READ ONLY`.

   Tests that prove isolation MUST connect as `app_user`.
4. **One request is one transaction.** It starts with `SELECT set_config('app.org_id', $1, true), set_config('app.user_id', $2, true), set_config('app.role', $3, true)`. Never use plain `SET`: it is session-level and leaks across pooled connections.
5. **Tools receive a `ToolContext(org_id, user_id, role, conversation_id, turn_id)` injected by the server.** Tool argument schemas the LLM sees NEVER contain org_id, user_id or customer_id. The tool registry is chosen by `org.vertical`.
6. **Mutating tools only create proposals.** These are `create_order`, `update_order`, `cancel_order`, `reorder`, `create_booking`, `reschedule_booking` and `cancel_booking`; each writes a `proposals` row and returns a summary card.
   - Only `confirm_proposal(proposal_id)`, or `POST /api/proposals/{id}/confirm` from the card button, commits.
   - The server rejects confirmation when the proposal was created in the current turn, so a user message must come in between.
   - On commit, price, stock and slot freshness are re-checked; if anything changed, the proposal is superseded and a new one issued.
   - Note: confirming a proposal creates an order with status `pending`. The order status `confirmed` is a later store-side transition. Don't conflate the two.
7. **Foreign or missing resources return 404 with an identical body.** A customer calling an admin route gets 403.
8. **Live facts come only from database tools.** Prices, stock, order status and availability are never taken from the KB. KB markdown must contain no prices or stock figures; `make seed` lints for currency strings and fails if it finds any.
9. **Times are stored as UTC `timestamptz`** and shown in the org's timezone (Asia/Kathmandu, UTC+05:45). All "now" goes through `app.clock.now()` so tests and evals can freeze it.
10. **No secrets in the repo.** Every new env var is added to `.env.example` in the same commit.
11. **Behaviour is keyed on `org.vertical`, never on the slug.** Adding a third org must take config plus seed data only, with no code changes.
12. **No agent framework.** `agents/loop.py` stays under 200 lines with a comment on every non-obvious line; the author must be able to explain it line by line.

## Working rules for Claude Code

- Use plan mode before each milestone, and show the plan before writing code.
- For isolation and business rules, write tests first, then the implementation. Run `make test` before saying anything is done.
- Never weaken, skip or delete a failing test to make it pass. Report it instead.
- State why before adding any dependency.
- Make small commits per feature, using conventional-commit messages.
- After each milestone, append 2–3 lines to `docs/ai-usage.md` on what the AI did and what was reviewed by hand. The README needs this.

## Commands

```
make up        # docker compose up -d (db, backend, frontend)
make migrate   # alembic upgrade head (as app_owner)
make seed      # rebuild both orgs' relational data and KB vectors from seed/
make test      # pytest (deterministic, no LLM)
make eval      # LLM scenario suite → eval/results.md
make advance ORDER=<uuid>   # move an order to its next status (demo/admin)
```

## Domain rules

**Orders**
- Status flow: `pending → confirmed → shipped → delivered`. `cancelled` is reachable only from `pending`. Every transition writes a row to `order_status_history`.
- Stock is decremented atomically in one transaction (`UPDATE … SET stock = stock - q WHERE id = … AND stock >= q`; any rowcount of 0 aborts the whole transaction). It is restored on cancel or when an edit lowers a quantity.
- Edits and cancellations take `SELECT … FOR UPDATE` on the order, then check that it is `pending`. A refusal includes the current status.
- Payment is mocked as cash on delivery.

**Bookings**
- Slots move in 15-minute steps. A slot must sit inside opening hours, avoid holidays, and fit the service's full duration before closing.
- Double-booking is prevented by Postgres EXCLUDE constraints on `tstzrange(starts_at, ends_at, '[)')`, one per practitioner and one per customer, for `status = 'confirmed'`. Catch `exclusion_violation` and offer the 3 nearest free slots by time difference.
- Customers can't see other customers' bookings because of RLS, so the slot engine reads busy intervals through the SECURITY DEFINER function `busy_intervals()`. It returns only (practitioner_id, starts_at, ends_at).
- Cancellation is free more than 24 hours ahead; inside 24 hours it costs a 50% fee (mocked, recorded on the booking).
- "Morning" is 09:00–12:00, "afternoon" 12:00–17:00, "evening" 17:00–close. Relative dates are resolved by `domain/slots/dates.py` against `clock.now()` in the org's timezone. Confirmation cards always show the absolute date.

**Retrieval**
- Chunking: one FAQ Q&A per chunk; one product per chunk; policy documents split at each `##` heading, with overlap only for sections over 400 tokens.
- Each chunk is cited as `doc_id#section`.
- Search is hybrid: pgvector cosine + `tsvector` full-text, merged with reciprocal rank fusion (k=60).
- If nothing passes the score threshold, the tool returns `{"results": [], "answerable": false}` and the agent says it doesn't know.
- The server validates that every citation the model returns was in the retrieved set.

**Rate limiting**
- Chat is limited to 20 messages/minute per user and returns 429 with `Retry-After`.
- LLM calls go through a global async limiter per provider (Mistral free tier: 1 request/second) with exponential backoff on 429.

## Gotchas

- RLS doesn't apply to superusers, and the table owner bypasses it unless `FORCE` is set. Never run the app or the isolation tests as `postgres` or `app_owner`.
- An unset `current_setting('app.org_id', true)` returns NULL or an empty string. The `NULLIF(...,'')::uuid` makes policies deny instead of raising an error.
- asyncpg doesn't accept bind parameters in `SET`; use `SELECT set_config(...)`.
- An EXCLUDE constraint on a uuid with `=` needs `btree_gist`. Use `'[)'` ranges so back-to-back slots don't conflict.
- The `sessions` table has no RLS. It's looked up only by SHA-256 token hash, and stores `org_id`, `user_id` and `role` so it doesn't need to join `users` before the org is known.
- SSE: send the `X-Accel-Buffering: no` header, a heartbeat every 15 seconds, and stop the generator on client disconnect.
