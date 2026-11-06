import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.auth import routes as auth_routes
from app.auth.csrf import OriginCheckMiddleware
from app.config import get_settings
from app.db.session import app_engine, readonly_engine
from app.tenancy.orgs import check_orgs_present, load_org_configs


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Read-only startup check as app_user: the app never writes orgs (make sync-orgs does).
    configs = load_org_configs(get_settings().seed_dir)
    async with app_engine.connect() as conn:
        await check_orgs_present(conn, configs)
    yield
    await app_engine.dispose()
    await readonly_engine.dispose()


# uvicorn configures only its own loggers; this makes app.* INFO lines (e.g. the LLM
# budget: "gemini/<model>: 37/450 requests today, 413 left") visible in `make logs`.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Multi-Tenant Agent Platform", lifespan=lifespan)
app.add_middleware(OriginCheckMiddleware, allowed_origins=get_settings().allowed_origin_set)
app.include_router(health.router)
app.include_router(auth_routes.router)
