"""Liveness endpoint, also used by the Docker healthcheck."""

import asyncio
import logging

import asyncpg
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db import engine as db

router = APIRouter()
logger = logging.getLogger(__name__)

# Shorter than the Docker healthcheck's own 4-second request timeout.
DB_CHECK_TIMEOUT_SECONDS = 3


@router.get("/api/health")
async def health() -> JSONResponse:
    try:
        async with asyncio.timeout(DB_CHECK_TIMEOUT_SECONDS):
            async with db.app_engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, asyncpg.PostgresError, OSError, TimeoutError) as exc:
        # Log the type only: driver messages can include connection details.
        logger.warning("Health check database query failed: %s", type(exc).__name__)
        return JSONResponse({"ok": False, "db": False}, status_code=503)
    return JSONResponse({"ok": True, "db": True})
