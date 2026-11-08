import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import logging_setup
from app.api import admin, chat, conversations, health, kb, panels
from app.auth import routes as auth_routes
from app.auth.csrf import OriginCheckMiddleware
from app.config import get_settings
from app.db.session import app_engine, readonly_engine
from app.domain.kb import embeddings
from app.domain.notifications import worker as email_worker
from app.observability import tracing
from app.tenancy.orgs import check_orgs_present, load_org_configs


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Read-only startup check as app_user: the app never writes orgs (make sync-orgs does).
    configs = load_org_configs(get_settings().seed_dir)
    async with app_engine.connect() as conn:
        await check_orgs_present(conn, configs)
    # Load the embedding model and run one embed now: uvicorn accepts no request until the
    # lifespan has started, and /api/health also requires it, so the Docker healthcheck
    # (and `up --wait`) only pass once the first search_knowledge is fast.
    await embeddings.warm_up()
    # Email sender + 24 h reminder scan (domain/notifications), only where the service
    # enables them: never in tests or evals, which call their run_once() directly.
    workers = email_worker.start() if get_settings().email_worker_enabled else []
    # OpenTelemetry: on only when OTEL_EXPORTER_OTLP_ENDPOINT is set (make up-traces).
    traces = tracing.setup_from_env()
    yield
    if traces is not None:
        traces.shutdown()  # flush the last spans to the collector
    for task in workers:
        task.cancel()
    await asyncio.gather(*workers, return_exceptions=True)
    await app_engine.dispose()
    await readonly_engine.dispose()


# JSON log lines for app.* and uvicorn (e.g. the LLM budget line), visible in `make logs`.
logging_setup.configure()  # JSON lines with request_id, route and org

app = FastAPI(title="Multi-Tenant Agent Platform", lifespan=lifespan)
app.add_middleware(OriginCheckMiddleware, allowed_origins=get_settings().allowed_origin_set)
# Added last, so it is outermost: even a CSRF 403 gets a request id.
app.add_middleware(logging_setup.RequestContextMiddleware, router_app=app)
app.include_router(health.router)
app.include_router(auth_routes.router)
app.include_router(conversations.router)
app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(kb.router)
app.include_router(panels.router)
