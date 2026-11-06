# AI usage

## Scaffold

Claude Code generated the Docker Compose stack, database roles, backend and frontend skeletons, and Makefile from a plan reviewed before any code was written.
It also ran the checks: role attributes, migration prerequisites as `app_owner`, the health endpoint during a database outage, and hot reload.
Reviewed by hand: TODO(Aditi) — note what you read and checked yourself.

## Schema and RLS

Claude Code ported schema_reference.sql into the first Alembic migration, wrote `tenant_tx`/`readonly_tx`/`anon_tx`, and wrote the 253 isolation, exclusion, pool-leak and race tests first, running them as `app_user` against a separate `app_test` database. The tests caught that the reference's REVOKE/GRANT on `busy_intervals()` silently did nothing once ownership moved to `rls_definer` (fixed by reordering), and that racing EXCLUDE inserts can fail with `deadlock_detected` as well as `exclusion_violation`.
Reviewed by hand: TODO(Aditi) — note what you read and checked yourself (e.g. the two HARDENING additions in the migration, and the privilege-order fix).

## Org config and auth

Claude Code added seed `org.yaml` loading, a one-shot `ops` container so the running backend never holds `app_owner` credentials, a DB trigger that blocks admin creation outside an admin context, and argon2id cookie-session auth with an Origin check, writing the isolation tests first (285 tests green). The trigger tests also exposed that a customer could previously promote their own `users` row to admin.
Reviewed by hand: TODO(Aditi) — note what you checked yourself (e.g. the dummy-hash verify on login misses, cookie flags, the CSRF middleware, and the `users_admin_guard` trigger).

## Seed data and retrieval

Claude Code wrote the fictional seed content (22 products, 7 services, 41 FAQ answers, policies and one planted prompt-injection fixture), the idempotent `make seed` (validate, lint, chunk and embed before one owner transaction under FORCE RLS; `SEED_NOW` pins relative dates) and hybrid pgvector + full-text search with RRF, writing the 52 tests first (337 green). It calibrated the answerability threshold at cosine 0.68 from 10 in-scope (0.744–0.889) vs 10 out-of-scope (0.392–0.619) queries and found that the BGE query prefix made no real difference.
Reviewed by hand: TODO(Aditi): note what you checked yourself (e.g. the seed content for consistency, the lint patterns, the RRF SQL and the wipe order).
Recalibrated to 0.625 after testing casual phrasings; near-domain questions can't be separated by score, so the model and evals handle them.
