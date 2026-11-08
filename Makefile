# Everything runs inside the containers, so the host needs only Docker and make.
.PHONY: help lint up up-traces build down migrate sync-orgs seed kb-search chat testdb test test-live record-fixtures llm-budget create-admin ephemeral-db evaldb eval eval-live e2e e2e-setup advance logs

help:  ## List the make targets
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/^([a-zA-Z_-]+):.*## (.*)/\1\t\2/' | awk -F'\t' '{printf "  %-16s %s\n", $$1, $$2}'

# Static checks, no database or LLM: ruff (lint + format check) and mypy on the backend,
# eslint and tsc on the frontend. One-shot containers, so nothing needs to be running.
lint:  ## ruff, ruff format --check, mypy (backend); eslint, tsc (frontend)
	docker compose run --rm --no-deps -T ops sh -c 'ruff check app scripts tests && ruff format --check app scripts tests && mypy app'
	docker compose run --rm --no-deps -T frontend sh -c 'npm run -s lint && npm run -s typecheck'

# Owner-only commands run in the one-shot `ops` container, which is the only place
# DATABASE_URL_OWNER exists. The running backend never holds app_owner credentials.
OPS = docker compose run --rm ops

# Uses existing images; it never rebuilds. Compose builds an image only if it is missing.
# migrate and sync-orgs run first (both idempotent): the backend refuses to start if an
# org.yaml slug is missing from the orgs table.
up: migrate sync-orgs  ## Start db, backend and frontend, then wait until healthy
	docker compose up -d --wait

# -V replaces the anonymous node_modules volume, which would otherwise keep the old
# dependencies hidden behind the new image.
# Same as `up`, plus Jaeger (compose profile "observability", off by default to save RAM),
# with the backend exporting OpenTelemetry spans to it. A later plain `make up` turns
# tracing off again (the backend is recreated without the endpoint).
up-traces: migrate sync-orgs  ## Start everything plus Jaeger; traces at http://localhost:16686
	OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318 docker compose --profile observability up -d --wait
	@echo "Jaeger UI: http://localhost:16686 (service multitenant-backend)"

build:  ## Rebuild images after a dependency change, then restart on them
	docker compose build
	docker compose up -d --wait -V

down:  ## Stop the services (keeps the database volume)
	docker compose --profile observability down

migrate:  ## alembic upgrade head, as app_owner
	$(OPS) alembic upgrade head

sync-orgs:  ## Upsert the orgs table from seed/<slug>/org.yaml, as app_owner
	$(OPS) python -m app.tenancy.sync

# Rebuilds every org's tenant rows and KB vectors from seed/ (idempotent). SEED_NOW pins
# the "now" that relative seed dates resolve against: make seed SEED_NOW=2026-11-09T10:00
seed: sync-orgs  ## Wipe and reseed each org's data and knowledge base, as app_owner
	docker compose run --rm $(if $(SEED_NOW),-e SEED_NOW="$(SEED_NOW)") ops python -m app.seed

# Runs as app_user in the backend container, exactly like the agent's search tool.
kb-search:  ## make kb-search ORG=<slug> Q="question"
	@test -n "$(ORG)" -a -n "$(Q)" || (echo 'usage: make kb-search ORG=<slug> Q="..."' && exit 1)
	docker compose exec backend python -m app.domain.kb.cli --org "$(ORG)" "$(Q)"

# One real chat turn through the running backend's HTTP API (spends LLM requests).
# DEMO ONLY: the password is on the command line; use a seeded demo account.
# CONV=<id> continues a conversation (its id is printed on the first line). ADMIN=1 uses the
# admin analytics chat (an admin account, e.g. admin@himali.test).
chat:  ## make chat ORG=<slug> EMAIL=<email> PASSWORD=<pw> Q="question" [CONV=<id>] [ADMIN=1]
	@test -n "$(ORG)" -a -n "$(EMAIL)" -a -n "$(PASSWORD)" -a -n "$(Q)" || (echo 'usage: make chat ORG=<slug> EMAIL=<email> PASSWORD=<pw> Q="..." [CONV=<id>]' && exit 1)
	docker compose exec backend python -m scripts.chat --org "$(ORG)" --email "$(EMAIL)" --password "$(PASSWORD)" $(if $(CONV),--conversation "$(CONV)") $(if $(ADMIN),--admin) "$(Q)"

# Tests and evals use db-ephemeral (compose profile "ephemeral"): tmpfs data, durability
# off, never the dev `db`. Its init scripts create the roles, app_test and eval_template on
# every fresh start; testdb/evaldb re-apply them idempotently to a running instance.
EPHEMERAL = docker compose --profile ephemeral
PSQL_EPHEMERAL = $(EPHEMERAL) exec -T db-ephemeral sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

ephemeral-db:  ## Start the throwaway Postgres for tests and evals (idempotent)
	$(EPHEMERAL) up -d --wait db-ephemeral

testdb: ephemeral-db  ## Ensure app_test exists on db-ephemeral (idempotent)
	$(PSQL_EPHEMERAL) < docker/db/testdb.sql

# The suite rebuilds app_test with alembic (downgrade base, upgrade head) on every run.
# Isolation tests still connect through the app_user engine.
test: testdb  ## pytest against app_test on db-ephemeral (deterministic, no LLM)
	$(EPHEMERAL) run --rm test pytest

# Real LLM calls, one request per test, in the backend container (which holds the keys).
# Never part of `make test`; a provider whose key is missing is skipped.
test-live:  ## Smoke-test the live LLM providers (spends ~1 request each)
	docker compose exec backend pytest tests_live -q -rs

# Re-records the Gemini HTTP fixtures under backend/tests/fixtures/llm/gemini (3 requests).
record-fixtures:  ## Record real Gemini responses for the adapter tests (spends 3 requests)
	docker compose exec backend python -m scripts.record_llm_fixtures

llm-budget:  ## Show today's LLM requests per model against the daily budget
	docker compose exec backend python -m scripts.llm_budget

# Sign-up only creates customers. This prompts for the password and runs as app_user in
# an admin-scoped transaction, so it needs no owner credentials.
create-admin:  ## make create-admin ORG=<slug> EMAIL=<email>
	@test -n "$(ORG)" -a -n "$(EMAIL)" || (echo "usage: make create-admin ORG=<slug> EMAIL=<email>" && exit 1)
	docker compose exec backend python -m app.auth.create_admin --org "$(ORG)" --email "$(EMAIL)"

# UI smoke tests (Playwright, run from WSL against localhost) on the scripted fake LLM: no
# quota is spent. The backend is switched to LLM_PROVIDER=fake for the run and always put
# back afterwards. One-time setup: make e2e-setup.
E2E_PY = e2e/.venv/bin/python
e2e-setup:  ## Create e2e/.venv with Playwright and download Chromium (once)
	python3 -m venv e2e/.venv
	e2e/.venv/bin/pip install -q -r e2e/requirements.txt
	e2e/.venv/bin/playwright install chromium

e2e:  ## Playwright smoke tests + screenshots in docs/screenshots (fake LLM)
	@test -x $(E2E_PY) || (echo "run make e2e-setup first" && exit 1)
	docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d --wait backend frontend
	cd e2e && BASE_URL=http://localhost:3000 SCREENSHOT_DIR=../docs/screenshots .venv/bin/python -m pytest -q $(ARGS); \
	  status=$$?; cd .. && docker compose up -d --wait backend; exit $$status

# The superuser creates the clone-only app_eval role and eval_template (with pgvector) on
# db-ephemeral, as testdb does for app_test.
evaldb: ephemeral-db  ## Ensure app_eval and eval_template exist on db-ephemeral (idempotent)
	$(PSQL_EPHEMERAL) < docker/db/evaldb.sql

# LLM scenario suite (eval/scenarios) → eval/results.md and results.json. Prints the LLM
# budget (as make llm-budget) before and after. Each scenario runs
# on its own clone of a seeded template database, with the clock frozen.
#   make eval        replay a scenario's cassette if it exists, else record it (live)
#   make eval-live   record live, overwriting cassettes; ONLY=store/edit_shipped,booking/*
#                    limits it to some scenarios (never re-record the whole suite casually)
#   ARGS=--judge     add LLM-as-judge scores for KB-answer scenarios (reported separately)
EVAL = docker compose --profile ephemeral --profile eval run --rm eval
eval: evaldb  ## Run the LLM eval suite (replay where recorded, else record)
	$(EVAL) python /eval/runner.py --mode auto $(if $(ONLY),--only "$(ONLY)") $(ARGS)

eval-live: evaldb  ## Re-record eval scenarios live (ONLY=<vertical>/<id>[,...] strongly advised)
	$(EVAL) python /eval/runner.py --mode record $(if $(ONLY),--only "$(ONLY)") $(ARGS)

# Admin/demo: one status step (pending → confirmed → shipped → delivered). The prefix picks
# the org; runs as app_user in an admin-scoped transaction, so no owner credentials.
advance:  ## make advance ORDER=<order number, e.g. HT-1042>
	@test -n "$(ORDER)" || (echo "usage: make advance ORDER=HT-1042" && exit 1)
	docker compose exec backend python -m scripts.advance "$(ORDER)"

logs:  ## Follow logs from all services
	docker compose logs -f
