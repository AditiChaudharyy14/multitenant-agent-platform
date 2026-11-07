# Everything runs inside the containers, so the host needs only Docker and make.
.PHONY: up build down migrate sync-orgs seed kb-search chat testdb test test-live record-fixtures llm-budget create-admin evaldb eval eval-live e2e e2e-setup advance logs

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
build:  ## Rebuild images after a dependency change, then restart on them
	docker compose build
	docker compose up -d --wait -V

down:  ## Stop the services (keeps the database volume)
	docker compose down

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
# CONV=<id> continues a conversation (its id is printed on the first line).
chat:  ## make chat ORG=<slug> EMAIL=<email> PASSWORD=<pw> Q="question" [CONV=<id>]
	@test -n "$(ORG)" -a -n "$(EMAIL)" -a -n "$(PASSWORD)" -a -n "$(Q)" || (echo 'usage: make chat ORG=<slug> EMAIL=<email> PASSWORD=<pw> Q="..." [CONV=<id>]' && exit 1)
	docker compose exec backend python -m scripts.chat --org "$(ORG)" --email "$(EMAIL)" --password "$(PASSWORD)" $(if $(CONV),--conversation "$(CONV)") "$(Q)"

# app_owner has no CREATEDB, so the superuser creates app_test. The pytest session then
# rebuilds its schema with alembic (downgrade base, upgrade head) on every run.
testdb:  ## Create the separate app_test database if it is missing (idempotent)
	docker compose exec -T db sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' < docker/db/testdb.sql

# Runs in ops: the suite rebuilds app_test with alembic and writes fixtures as app_owner.
# Isolation tests still connect through the app_user engine.
test: testdb  ## pytest against app_test (deterministic, no LLM)
	$(OPS) pytest

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

# The superuser creates the clone-only app_eval role and the eval_template database (with
# pgvector), as testdb does for app_test. `up -d db` first so the db container has
# APP_EVAL_PASSWORD in its environment (it is recreated only if its config changed).
evaldb:  ## Create the app_eval role and the eval_template database (idempotent)
	docker compose up -d --wait db
	docker compose exec -T db sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' < docker/db/evaldb.sql

# LLM scenario suite (eval/scenarios) → eval/results.md and results.json. Prints the LLM
# budget (as make llm-budget) before and after. Each scenario runs
# on its own clone of a seeded template database, with the clock frozen.
#   make eval        replay a scenario's cassette if it exists, else record it (live)
#   make eval-live   record live, overwriting cassettes; ONLY=store/edit_shipped,booking/*
#                    limits it to some scenarios (never re-record the whole suite casually)
#   ARGS=--judge     add LLM-as-judge scores for KB-answer scenarios (reported separately)
EVAL = docker compose --profile eval run --rm eval
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
