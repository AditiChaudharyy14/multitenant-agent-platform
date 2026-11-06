"""Alembic environment. Migrations are raw SQL and always run as app_owner."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import require_owner_url

if context.config.config_file_name is not None:
    # Keep loggers created before migrations ran (the test session migrates in-process).
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)

# Read directly rather than via config.set_main_option, which would treat any % in
# the URL as ini interpolation.
OWNER_URL = require_owner_url()


def run_migrations(connection: Connection) -> None:
    # target_metadata is None: there are no ORM models to autogenerate from.
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(OWNER_URL, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=OWNER_URL, target_metadata=None, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
