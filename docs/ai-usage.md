# AI usage

## Scaffold

Claude Code generated the Docker Compose stack, database roles, backend and frontend skeletons, and Makefile from a plan reviewed before any code was written.
It also ran the checks: role attributes, migration prerequisites as `app_owner`, the health endpoint during a database outage, and hot reload.
Reviewed by hand: Reviewed the plan; caught that the wrong CLAUDE.md from another project had been copied in; directed a root-cause fix for Docker credential-helper and WSL DNS failures instead of retries, cutting warm start from ~3 min to ~12 s.

## Schema and RLS

Claude Code ported schema_reference.sql into the first Alembic migration, wrote `tenant_tx`/`readonly_tx`/`anon_tx`, and wrote the 253 isolation, exclusion, pool-leak and race tests first, running them as `app_user` against a separate `app_test` database. The tests caught that the reference's REVOKE/GRANT on `busy_intervals()` silently did nothing once ownership moved to `rls_definer` (fixed by reordering), and that racing EXCLUDE inserts can fail with `deadlock_detected` as well as `exclusion_violation`.
Reviewed by hand: Added three tests to the plan — a separate test database, a real 10-connection concurrency test, and a pooled-connection leak test; the tests caught a grant-ordering bug in my reference schema.

## Org config and auth

Claude Code added seed `org.yaml` loading, a one-shot `ops` container so the running backend never holds `app_owner` credentials, a DB trigger that blocks admin creation outside an admin context, and argon2id cookie-session auth with an Origin check, writing the isolation tests first (285 tests green). The trigger tests also exposed that a customer could previously promote their own `users` row to admin.
Reviewed by hand: Rejected owner DB credentials in the running app; moved them to a separate ops service and asked for a DB-level guard so signup can never create an admin.

## Seed data and retrieval

Claude Code wrote the fictional seed content (22 products, 7 services, 41 FAQ answers, policies and one planted prompt-injection fixture), the idempotent `make seed` (validate, lint, chunk and embed before one owner transaction under FORCE RLS; `SEED_NOW` pins relative dates) and hybrid pgvector + full-text search with RRF, writing the 52 tests first (337 green). It calibrated the answerability threshold at cosine 0.68 from 10 in-scope (0.744–0.889) vs 10 out-of-scope (0.392–0.619) queries and found that the BGE query prefix made no real difference.
Reviewed by hand: Challenged the 0.68 threshold as too tight for casual phrasing; recalibrated with ~30 realistic queries to 0.625 and checked that agent-style rewritten queries rank the right chunk first.
Recalibrated to 0.625 after testing casual phrasings; near-domain questions can't be separated by score, so the model and evals handle them.

## LLM provider layer

Mistral's free "Experiment" plan isn't available to this account (its API keys page says "Upgrade to activate"), so there is no `MISTRAL_API_KEY` and Gemini is the default provider. The Mistral adapter is complete but tested only against fixtures hand-built from Mistral's documented SSE format; its live test is skipped while the key is absent. Gemma 4 isn't a fallback because `models.list` shows no function-calling support.
Claude Code wrote the provider layer on plain httpx: neutral types, Gemini and Mistral adapters, a per-model RPM limiter, a Postgres-backed per-model daily budget with fallback, a FakeProvider for `make test`, and EVAL_MODE record/replay cassettes. It wrote the 45 offline tests (plus 2 live smoke tests) first, against 3 real Gemini responses it recorded (6 requests in total, counted by the budget). Recording showed that Gemini 3.5 returns its own call ids and thoughtSignatures, and that both must be echoed back. It also found that alembic's `fileConfig` silently disabled app loggers during tests.
Reviewed by hand: Found Mistral's free API plan unavailable to new accounts; read the real per-model Gemini quotas in AI Studio and designed the 3.5 / 3.1 Flash-Lite primary/fallback split around them.

## Tool framework, agent loop and chat API

Claude Code wrote the tool framework (registry keyed on vertical, `search_knowledge` with `<kb_source>` delimiters), the agent loop (167 lines, model pinned per turn via `pin()`, server-side citation filter), the cancellable turn service and the SSE chat API, writing the 40 tests first (436 green). It reweighted RRF 2:1 toward vectors and isolated the chat tests in fresh orgs after they polluted the seed idempotency test.
Reviewed by hand: Required model pinning per turn (no mid-turn fallback, since Gemini signatures are model-specific) and query rewriting in search_knowledge after the retrieval calibration showed raw phrasing misranks.

## Store agent: order life cycle

Claude Code added order numbers (per-org counter trigger, `HT-1042`), the shared proposal/confirm module, the store tools and committers (row locks, guarded stock decrements, re-quote on price/stock change), the deterministic Confirm-button path and `make advance`, writing the 106 new tests first (554 green). The live Gemini demo showed the model re-calling `confirm_proposal` after a button commit (the server refused it), so a button turn no longer offers that tool at all.
Reviewed by hand: Specified that every business rule is enforced in code with a test (same-turn confirm refused, price re-check at commit, concurrent last-unit orders); reviewed the plan before approving.

## Booking agent: slots, dates and appointments

Claude Code built the booking agent test-first (138 new tests, 692 green): a pure slot engine behind a `SlotProvider` interface, checked by Hypothesis against a brute-force oracle; the relative-date resolver; LW booking numbers on the order-counter pattern; proposal-only booking tools; and committers that re-check price, slot and fee and turn `exclusion_violation` (deadlocks retried once) into 3 nearest alternatives. Ten concurrent confirms make exactly one booking.
The live Gemini demo (45 requests today) showed the model passing dates in our own card format ("Tuesday 10 November afternoon", "2026-11-10 13:30"), which the resolver then rejected; those forms were added as tests and fixed. A cancellation inside 24 hours couldn't happen live, because the studio is closed from Friday 15:00 to Sunday 09:00, so tests cover the 50% fee.
Reviewed by hand: Defined the date-ambiguity rule (ask when "next <weekday>" is tomorrow) and required property tests for the slot engine.

## Chat UI core

Claude Code built the Next.js chat UI on the existing API: a same-origin `/api` proxy (Next rewrites, checked to stream SSE and pass Stop through to the server), branded landing with login/sign-up tabs, a gate that calls only `/api/me` and shows a "This account belongs to …" screen on an org mismatch, streaming chat with Stop, live tool status, markdown with highlighted code, citation chips and a source drawer.
It added `GET /api/kb/source` (RLS-scoped, one 404 for foreign, missing and malformed refs), a `quota_exhausted` error code, and a YAML-scripted `ScriptedProvider` (`LLM_PROVIDER=fake` + `FAKE_SCENARIO_FILE`) that the Playwright smoke tests (`make e2e`, run from a WSL venv after a Docker image proved too slow on this machine; screenshots in `docs/screenshots/`) run against. The tests fail on any browser console error, which caught a hydration mismatch caused by Playwright's own caret-hiding screenshot style.
Reviewed by hand: Required the cross-org "belongs to" screen as a visible demo moment and a fake-provider mode so UI tests spend no LLM quota.
