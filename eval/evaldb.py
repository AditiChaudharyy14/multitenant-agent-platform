"""Per-scenario databases: one seeded template, one cheap clone per scenario.

- eval_template is migrated and seeded once, as app_owner, with the clock frozen at
  EVAL_SEED_NOW. It is rebuilt only when its fingerprint changes (migrations, seed data,
  seeder/KB code or SEED_NOW), stored with COMMENT ON DATABASE.
- Org ids are fixed (uuid5 of the slug), so every seeded row id (uuid5 of org id and seed
  key) and everything derived from it is identical across rebuilds. Recorded cassettes
  stay valid as long as nothing the model sees changes.
- Each scenario gets CREATE DATABASE eval_s_... TEMPLATE eval_template (a file copy,
  well under a second), run as app_eval, which may create and drop databases and nothing
  else. The app inside the runner talks to the clone as app_user, exactly as in prod.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import pool, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import get_settings, require_owner_url
from app.seed.run import seed_all
from app.tenancy.orgs import load_org_configs, sync_orgs

TEMPLATE = "eval_template"
CLONE_PREFIX = "eval_s_"
ORG_NAMESPACE = uuid.UUID("2b0f6a63-9d0c-4a8e-8f43-0e3c3b7f5a10")  # fixed: org ids stable across rebuilds
BACKEND = Path("/app")
_FINGERPRINT_INPUTS = ("migrations/versions", "app/seed", "app/domain/kb", "app/tenancy")


def org_id_for(slug: str) -> uuid.UUID:
    return uuid.uuid5(ORG_NAMESPACE, slug)


def url_for(base: str, database: str) -> str:
    return make_url(base).set(database=database).render_as_string(hide_password=False)


def engine(url: str) -> AsyncEngine:
    # NullPool: no connection outlives its use, so a clone can always be dropped at once.
    return create_async_engine(url, poolclass=pool.NullPool)


def fingerprint(seed_dir: Path, seed_now: str) -> str:
    h = hashlib.sha256(f"seed_now={seed_now}\n".encode())
    files = [p for d in _FINGERPRINT_INPUTS for p in sorted((BACKEND / d).rglob("*.py"))]
    files += sorted(p for p in seed_dir.rglob("*") if p.is_file())
    for p in files:
        h.update(str(p).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:32]


async def ensure_template(seed_now: str) -> tuple[bool, str]:
    """Build eval_template if its fingerprint is stale. Returns (rebuilt, fingerprint)."""
    settings = get_settings()
    seed_dir = Path(settings.seed_dir)
    fp = fingerprint(seed_dir, seed_now)
    owner_url = url_for(require_owner_url(), TEMPLATE)
    eng = engine(owner_url)
    try:
        async with eng.connect() as conn:
            current = await conn.scalar(
                text("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = :d"), {"d": TEMPLATE}
            )
    finally:
        await eng.dispose()
    if current == fp:
        return False, fp

    # Rebuild from scratch: alembic in a subprocess (its env.py runs its own event loop)
    # against the template, then org rows with fixed ids, then the normal seeder.
    env = {**os.environ, "DATABASE_URL_OWNER": owner_url}
    for step in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run(["alembic", *step], cwd=BACKEND, env=env, check=True, capture_output=True, text=True)
    configs = load_org_configs(seed_dir)
    await sync_orgs(owner_url, configs)
    eng = engine(owner_url)
    try:
        async with eng.begin() as conn:
            for cfg in configs:  # nothing references orgs yet on a fresh schema
                await conn.execute(text("UPDATE orgs SET id = :id WHERE slug = :s"), {"id": org_id_for(cfg.slug), "s": cfg.slug})
    finally:
        await eng.dispose()
    await seed_all(owner_url, seed_dir, seed_now)
    eng = engine(owner_url)
    try:
        async with eng.begin() as conn:
            await conn.execute(text(f"COMMENT ON DATABASE {TEMPLATE} IS '{fp}'"))  # fp is hex only
    finally:
        await eng.dispose()
    return True, fp


def _admin_engine() -> AsyncEngine:
    url = os.environ.get("DATABASE_URL_EVAL")
    if not url:
        raise RuntimeError("DATABASE_URL_EVAL is not set: run evals through `make eval`")
    return create_async_engine(url, poolclass=pool.NullPool, isolation_level="AUTOCOMMIT")


def clone_name(key: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in key.lower())
    return (CLONE_PREFIX + safe)[:63]


async def drop_leftover_clones() -> list[str]:
    """Clones left by an interrupted run (only app_eval's own databases can be dropped)."""
    eng = _admin_engine()
    try:
        async with eng.connect() as conn:
            names = (await conn.execute(
                text("SELECT datname FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba "
                     "WHERE r.rolname = current_user AND datname LIKE :p"), {"p": CLONE_PREFIX + "%"}
            )).scalars().all()  # fmt: skip
            for name in names:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        return list(names)
    finally:
        await eng.dispose()


async def create_clone(name: str) -> None:
    eng = _admin_engine()
    try:
        async with eng.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            await conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE {TEMPLATE}'))
    finally:
        await eng.dispose()


async def drop_clone(name: str) -> None:
    eng = _admin_engine()
    try:
        async with eng.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        await eng.dispose()


@asynccontextmanager
async def owner_tx(eng: AsyncEngine, org_id: uuid.UUID) -> AsyncIterator[AsyncConnection]:
    """app_owner, acting as the org's admin. FORCE RLS applies to the owner too, so setup
    and assertions only ever see and touch the scenario's own org."""
    async with eng.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.org_id', :o, true), set_config('app.user_id', '', true), "
                 "set_config('app.role', 'admin', true)"), {"o": str(org_id)}
        )  # fmt: skip
        yield conn
