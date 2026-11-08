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

## Frontend part 2: rich cards, sidebar, My orders / My bookings

Claude Code replaced the card placeholders with real cards (confirmable order and booking summaries, slot chips grouped by day, status timeline, order/booking rows, stock badges), added inline rename and confirm-before-delete to the sidebar, and built the My orders / My bookings panel on `GET /api/orders` and `GET /api/bookings`. Backend first, test-first (722 green): both endpoints call the very functions the `list_my_*` tools call, with a test that the panel and the tool return identical items, and that another user's data never appears. A small `GET /api/conversations/{id}/proposals` read gives history cards their current status.
Playwright (fake provider, 1280 and 375 px) covers the order and booking flows through Confirm, rename and delete, and takes the card and panel screenshots in `docs/screenshots/`. The scripted model gained `{1}`-style capture groups so a script can say "status of order HT-1042".
Reviewed by hand: Required the panel to share the tools' domain functions so it can never disagree with the agent, and proposal-status-aware cards.

## Evals with database-state assertions

Claude Code built `make eval`. It runs 37 YAML scenarios (19 store, 18 booking) through the real FastAPI app, each on its own `CREATE DATABASE … TEMPLATE` clone of a template seeded with a frozen SEED_NOW, with the agent's clock frozen. Assertions check order, stock, booking and fee rows as well as tools, citations and replies. Cassettes are recorded once from Gemini, scrubbed and checked for secrets by a test, then replayed offline in ~75–100 s. Reports go to `eval/results.md`/`.json`, with the model answering each scenario, and an optional LLM judge is reported separately.
The first live run (31/37) exposed a real booking-prompt bug: exact-time requests never reached `create_booking`, so refusals lacked a reason and alternatives. It also exposed non-deterministic proposal and turn ids, and a daily budget that followed the frozen clock. All were fixed; three scenario mistakes were corrected and are documented in `eval/FAILURES.md`. Only booking cassettes were re-recorded (≈180 requests in total today).
Reviewed by hand: Designed the scenario list around every business rule and required DB-state assertions, frozen clocks and per-scenario template databases.
Follow-up: tests and evals moved to a `db-ephemeral` Postgres (tmpfs, durability off), and the suite and runner refuse any other host. Three more platform bugs surfaced and were fixed: random KB chunk ids reordered tied search results, a transient Gemini 503 wasn't retried, and a failed call shifted later calls in its cassette. One genuine agent failure remains, a "your order is on its way" for a pending order, explained in `eval/FAILURES.md`.
The last open eval failure (a "your order is on its way" for a pending order) was fixed test-first below the LLM: an "already confirmed" refusal now names the order or booking and its live status. 37/37 pass.

## Admin analytics agent (stretch)

Claude Code built a separate, read-only analytics chat for admins at `/a/<slug>/admin` (API `/api/admin/conversations…`, `require_admin` on every route, plus a RESTRICTIVE `admin_kind` RLS policy so no non-admin context can see or create one). Its registry holds only 14 fixed metric tools (orders/revenue/AOV/top products/out-of-stock; bookings/cancellations/utilisation/top services/busiest slots; active customers, sign-ups, conversion, AI usage with cost estimated at paid-tier prices). Each runs as `app_readonly` in a READ ONLY transaction scoped by RLS. Windows (today, this week from Monday, …) are resolved by the server at local midnights in the org timezone, and every result carries its window and definition, shown as the metric card's footnote. A new `stock_events` table records every shortage. `seed/<slug>/analytics.yaml` adds three weeks of history after the base rows; the eval template leaves it out so the 37 customer cassettes still replay. Tests first (1051 green): every metric against hand-computed values, the 23:50 NPT boundary, the conversion formula, 403 on every admin route, writes through the admin connection raise, zero rows of another org. Seven admin evals (DB ground truth computed by the harness, a refusal with DB unchanged, a customer 403) were recorded live with 11 requests: 44/44 pass.
The tests caught a utilisation bug (`least()` ignores NULLs, so an idle practitioner counted as fully booked), and the live demo caught the model reporting "0.0%" on a closed day. Both were fixed below the LLM: a FILTER in the query, and the tool now says the studio was closed and the rate is undefined.
Reviewed by hand: Required fixed read-only metric tools over a read-only DB role instead of text-to-SQL, with window and definition on every number.

## Admin "How did we do?" summary

Claude Code added `activity_summary`, an admin tool that answers a broad "how did we do?" in one read-only call. Its sections are chosen by `org.vertical`: orders by status, revenue and AOV (store) or bookings by status and cancellations with fees (booking), plus active customers, new sign-ups and conversion. Each section is the existing metric's own `compute()` output, with its window and definition, so the summary adds no numbers of its own. Tests first: every section equals the stand-alone metric, and nothing from another org gets in. Adding a tool changes every admin cassette's fingerprint, so `make eval` now reports such cassettes as STALE with one banner, runs replays before any recording, never records live while anything is stale, and leaves `results.md` untouched. The admin cassettes are re-recorded in one batch later.

## Third org: config and seed only

Claude Code added `seed/kathmandu-spice/`: a store with prefix KS, its own colours, greeting and emoji, 8 products, 3 orders, 2 customers and 1 admin, and a KB of about, shipping, a products catalogue and an 18-question FAQ (the existing KB structure test requires at least 18). The commit changes no app code. A tests-only commit before it keyed the seed fixtures by slug (a second store org would have collided), added `tests/isolation/test_all_seed_orgs.py`, which parametrises over every `seed/<slug>` at collection time, and made a reseed test independent of test order. An e2e test signs up at `/a/kathmandu-spice` and gets an answer cited from its own KB.

## Long-term customer memory

Claude Code added `customer_memory` (migration 0008: org_id, user_id, key, value, source stated|derived; ENABLE and FORCE RLS with the standard tenant and RESTRICTIVE own-rows policies; no `app_readonly` grant), the domain module `app/domain/memory`, and three tools: `remember_preference`, `forget_preference` and `get_my_preferences`, none of which takes an identity argument. Derived facts are computed live from the customer's own non-cancelled history, keyed on vertical (store: the 3 most-ordered products with SKUs and no prices; booking: the usual practitioner and service). Forgetting one stores a tombstone, so it stays hidden. At turn start the server appends a capped `<customer_memory>` block, framed as data, built in the customer's own RLS scope. Admin turns never get it. The panel has a "What I remember" section with a Forget button (`GET`/`DELETE /api/memories`, with the same 404 for a missing or foreign key). Org flag `features.memory` in org.yaml, on by default. Tests first: RLS as `app_user`, derivations, prompt sanitising, 404 parity, a FakeProvider turn in a brand-new conversation, and an e2e test for the panel. Three new eval scenarios are written and wait for the batch re-record, along with every store, booking and admin cassette, all of which are stale because the system prompt and tool list changed.

## Email notifications (local, Mailpit)

Claude Code added a transactional outbox (migration 0009: `email_outbox` with a unique `(org_id, dedupe_key)` and RLS. Only admin-scoped transactions can read, update or delete it, and a customer-scoped one may only insert a row addressed to themselves) plus `bookings.reminder_sent_at`. The order-shipped and booking-confirmed (and rescheduled) emails are queued in the same transaction as the change. A reminder scan every 5 minutes queues one email per confirmed booking within 24 h; it's idempotent through `reminder_sent_at`, a dedupe key that includes the start time, and `FOR UPDATE SKIP LOCKED`. A background sender (stdlib `smtplib` in a thread, no new dependency) delivers to Mailpit (`:8025`) with exponential backoff. Both loops run in the backend lifespan only when `EMAIL_WORKER_ENABLED`. Templates are branded from org config. Tests first: outbox rows on each trigger, no duplicates, a reschedule gets its own reminder, none for a cancelled booking, customers can't read the outbox, and delivery to an in-test SMTP stub. The tests caught a real RLS subtlety: `INSERT … ON CONFLICT DO NOTHING` needs the SELECT policy, so the dedupe is a plain insert in a savepoint. `make advance` to shipped now shows a branded email in Mailpit.

## Tracing (OpenTelemetry + Jaeger)

Claude Code added `app/observability/tracing.py`: one `agent.run` span per turn (org slug, vertical, conversation and turn ids, agent, status, step and tool counts), a child `llm.call` per model call from a provider wrapper (model, input/output tokens, finish reason), and a child `tool.call` per tool from a handler wrapper (name, duration, ok/error). `loop.py` is unchanged. Attributes are ids, names and numbers only: no email, user id, message text, tool arguments or results, and an exception is recorded by type only. The OTLP/HTTP exporter is built only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Jaeger all-in-one (in-memory, 384 MB cap) sits behind the compose profile `observability`, which is off by default, and `make up-traces` starts it with the backend pointed at it. Tests use an in-memory span exporter and check the tree, the parent links, an error status for a failed tool, and an attribute allow-list with no user text or email. Verified live on the fake provider: the Jaeger trace showed a cold embedding model taking 30 s inside `search_knowledge`. Note that FastAPI also emits its own HTTP spans (route and path only) once OpenTelemetry is installed.
Reviewed by hand: Chose derived + stated memory with a visible delete control, outbox-based email with idempotent reminders, and Jaeger behind an opt-in profile to protect the 8 GB dev machine.

## Hardening pass (before the final eval re-record)

KB consistency: `make kb-check` (also run inside `make seed`, which fails on a finding) checks that KB statements about open and closed weekdays and opening times match `data.yaml`'s `opening_hours`, and that every KB sentence mentioning one of an org's `canonical_facts` (return windows, refund processing, delivery times, the cancellation window, the fee %, the late-arrival minutes) states the canonical value. I reviewed the KB for contradictions by hand and found none; the check now keeps it that way.
Also in this pass, test-first and one commit each:
- live-data freshness: prompt rule 8, plus a "call again" line on every live-data tool;
- stored prompt injection: instruction-like memories are refused, and memories and addresses reach the model wrapped as `<user_data>`, which prompt rule 9 declares untrusted;
- input limits: 2000-character messages, order bounds, a history token budget;
- a per-conversation advisory turn lock (409 "A reply is still in progress");
- production mode by default for `make up` (`make dev` for hot reload);
- security headers on the API and the pages, and an XSS e2e test on the production build;
- a not-found page and a clear expired-session message;
- the `.env.example` drift test;
- `make stats`;
- gitleaks (0 findings across the full history), a backend lockfile, pip-audit and npm audit (one build-time PostCSS advisory accepted in `docs/security.md`), and non-root images;
- a chat log announced once each reply is complete;
- `make lint` (ruff, mypy, eslint, tsc) and structured JSON logs with a request id.

Two slips worth recording: after a `make e2e` run the backend was back on the live model, and two hand-run probes spent 4 live requests. `make e2e` now refuses to start unless the backend is on the fake provider.

### Next week

- A subagent code review over `backend/app` and `frontend/` for dead code, duplication, naming, long functions and missing docstrings. Dropped from this milestone for time.
- A full axe-core accessibility sweep (all three orgs; landing, chat and admin; 1280 and 375 px), fixing every serious and critical issue, including brand-colour contrast. This milestone shipped only the live-log semantics and the visible-focus check.
- Return keyboard focus to the message box when a reply finishes (today it falls back to the page).
- Upgrade to Next 16 to clear the PostCSS advisory.
- The structured JSON logs (request id, route, org; no message text, emails or passwords) were finished and pushed before the re-prioritisation, so they are in, not deferred.

Reviewed by hand: Ran a strict-reviewer audit of the brief and added freshness, stored-injection, input-limit, turn-lock, KB-consistency, security-header, accessibility and supply-chain hardening before the final eval re-record.

## Final eval run (2026-11-08)

Claude added six last scenarios: a booking that is never confirmed, a price changed between turns, an address-only edit, a stored "100% off" injection, the return window, and a message over the length limit. It also added the harness support they needed (per-turn setup, refused turns, `add_order`, scenario categories) and a quota floor of 120 requests counted across both models.
It ran one full live re-record (48/53), and a network timeout it surfaced became a router fix (`fix(llm)`). After I decided each remaining failure by hand, the result was 53/53:
- one regex widened;
- `stock_short` given a stricter, database-checked "names a real alternative" assertion;
- two scenarios fixed and re-recorded alone.

It then ran pass^3 on the 41 rule and safety scenarios (39/41, 121/123 runs). It extended the judge to four criteria on every reply, calibrated it against six deliberately bad replies (6/6 caught), and fixed a false positive (uncited database facts) that the good replies exposed. It added GitHub Actions: lint, tests, typecheck and offline eval replay on every push, with e2e on demand. Flakes, judge findings and deferred fixes are in `eval/FAILURES.md`.

Reviewed by hand: Required a single final re-record after all prompt changes, pass^3 reliability on rule and safety scenarios, and a judge calibrated against deliberately bad answers.
