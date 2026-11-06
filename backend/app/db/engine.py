"""Database engines. Creating an engine doesn't connect; connections open on first use."""

from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings

# Runtime engine: connects as app_user, which is subject to RLS.
app_engine = create_async_engine(get_settings().database_url_app, pool_pre_ping=True)
