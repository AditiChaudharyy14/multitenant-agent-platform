from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.db.engine import app_engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await app_engine.dispose()


app = FastAPI(title="Multi-Tenant Agent Platform", lifespan=lifespan)
app.include_router(health.router)
