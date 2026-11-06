# Everything runs inside the containers, so the host needs only Docker and make.
.PHONY: up build down migrate sync-orgs seed testdb test create-admin eval advance logs

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

seed: sync-orgs  ## Stub until the seed data exists
	$(OPS) echo "seed: not implemented yet"

# app_owner has no CREATEDB, so the superuser creates app_test. The pytest session then
# rebuilds its schema with alembic (downgrade base, upgrade head) on every run.
testdb:  ## Create the separate app_test database if it is missing (idempotent)
	docker compose exec -T db sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' < docker/db/testdb.sql

# Runs in ops: the suite rebuilds app_test with alembic and writes fixtures as app_owner.
# Isolation tests still connect through the app_user engine.
test: testdb  ## pytest against app_test (deterministic, no LLM)
	$(OPS) pytest

# Sign-up only creates customers. This prompts for the password and runs as app_user in
# an admin-scoped transaction, so it needs no owner credentials.
create-admin:  ## make create-admin ORG=<slug> EMAIL=<email>
	@test -n "$(ORG)" -a -n "$(EMAIL)" || (echo "usage: make create-admin ORG=<slug> EMAIL=<email>" && exit 1)
	docker compose exec backend python -m app.auth.create_admin --org "$(ORG)" --email "$(EMAIL)"

eval:  ## Stub until the eval suite exists
	docker compose exec backend echo "eval: not implemented yet"

advance:  ## Stub: make advance ORDER=<uuid>
	docker compose exec backend echo "advance: not implemented yet (ORDER=$(ORDER))"

logs:  ## Follow logs from all services
	docker compose logs -f
