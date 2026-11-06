# AI usage

## Scaffold

Claude Code generated the Docker Compose stack, database roles, backend and frontend skeletons, and Makefile from a plan reviewed before any code was written.
It also ran the checks: role attributes, migration prerequisites as `app_owner`, the health endpoint during a database outage, and hot reload.
Reviewed by hand: TODO(Aditi) — note what you read and checked yourself.

## Schema and RLS

Claude Code ported schema_reference.sql into the first Alembic migration, wrote `tenant_tx`/`readonly_tx`/`anon_tx`, and wrote the 253 isolation, exclusion, pool-leak and race tests first, running them as `app_user` against a separate `app_test` database. The tests caught that the reference's REVOKE/GRANT on `busy_intervals()` silently did nothing once ownership moved to `rls_definer` (fixed by reordering), and that racing EXCLUDE inserts can fail with `deadlock_detected` as well as `exclusion_violation`.
Reviewed by hand: TODO(Aditi) — note what you read and checked yourself (e.g. the two HARDENING additions in the migration, and the privilege-order fix).
