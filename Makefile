# Everything runs inside the containers, so the host needs only Docker and make.
.PHONY: up build down migrate seed test eval advance logs

# Uses existing images; it never rebuilds. Compose builds an image only if it is missing.
up:  ## Start db, backend and frontend, then wait until healthy
	docker compose up -d --wait

# -V replaces the anonymous node_modules volume, which would otherwise keep the old
# dependencies hidden behind the new image.
build:  ## Rebuild images after a dependency change, then restart on them
	docker compose build
	docker compose up -d --wait -V

down:  ## Stop the services (keeps the database volume)
	docker compose down

migrate:  ## alembic upgrade head, as app_owner
	docker compose exec backend alembic upgrade head

seed:  ## Stub until the seed data exists
	docker compose exec backend echo "seed: not implemented yet"

test:  ## pytest (deterministic, no LLM)
	docker compose exec backend pytest

eval:  ## Stub until the eval suite exists
	docker compose exec backend echo "eval: not implemented yet"

advance:  ## Stub: make advance ORDER=<uuid>
	docker compose exec backend echo "advance: not implemented yet (ORDER=$(ORDER))"

logs:  ## Follow logs from all services
	docker compose logs -f
