"""Org config: seed/<slug>/org.yaml is the source, the `orgs` table is the runtime copy.

`sync_orgs` writes the table and runs only as app_owner, from `make sync-orgs` (and the
test fixtures). The running app only reads `orgs` as app_user, and at startup checks
that every configured org is present. Nothing here, or anywhere in app/, knows a slug:
adding an org is a new seed folder plus `make sync-orgs`.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

_HEX_COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class Branding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_color: str
    secondary_color: str
    logo_emoji: str
    agent_name: str
    greeting: str

    @field_validator("primary_color", "secondary_color")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not _HEX_COLOUR.match(v):
            raise ValueError("must be #RRGGBB")
        return v


class OrgConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    vertical: Literal["store", "booking"]
    timezone: str
    # One line for the system prompt, e.g. "online tea shop selling Himalayan tea".
    business_type: str = ""
    branding: Branding

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError("slug must be lowercase letters, digits and single hyphens")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v


def load_org_configs(seed_dir: str | Path) -> list[OrgConfig]:
    """Parse every <seed_dir>/<slug>/org.yaml. The folder name must equal the slug."""
    root = Path(seed_dir)
    files = sorted(root.glob("*/org.yaml"))
    if not files:
        raise RuntimeError(f"no org.yaml found under {root}")
    configs = []
    for path in files:
        cfg = OrgConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if cfg.slug != path.parent.name:
            raise RuntimeError(f"{path}: slug {cfg.slug!r} does not match folder name")
        configs.append(cfg)
    return configs


async def sync_orgs(owner_url: str, configs: list[OrgConfig]) -> None:
    """Upsert one orgs row per config. app_owner only: app_user has SELECT on orgs."""
    engine = create_async_engine(owner_url, poolclass=pool.NullPool)
    try:
        async with engine.begin() as conn:
            for cfg in configs:
                await conn.execute(
                    text(
                        "INSERT INTO orgs (slug, name, vertical, timezone, business_type, branding) "
                        "VALUES (:slug, :name, :vertical, :tz, :business_type, CAST(:branding AS jsonb)) "
                        "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, "
                        "vertical = EXCLUDED.vertical, timezone = EXCLUDED.timezone, "
                        "business_type = EXCLUDED.business_type, branding = EXCLUDED.branding"
                    ),
                    {
                        "slug": cfg.slug,
                        "name": cfg.name,
                        "vertical": cfg.vertical,
                        "tz": cfg.timezone,
                        "business_type": cfg.business_type,
                        "branding": json.dumps(cfg.branding.model_dump()),
                    },
                )
    finally:
        await engine.dispose()


async def check_orgs_present(conn: AsyncConnection, configs: list[OrgConfig]) -> None:
    """Fail fast at startup if a configured org was never synced into the database."""
    present = set(await conn.scalars(text("SELECT slug FROM orgs")))
    missing = sorted(c.slug for c in configs if c.slug not in present)
    if missing:
        raise RuntimeError(
            f"org.yaml slugs missing from orgs table: {missing}; run `make sync-orgs`"
        )


@dataclass(frozen=True, slots=True)
class Org:
    id: UUID
    slug: str
    name: str
    vertical: str
    timezone: str
    branding: dict[str, Any]
    business_type: str = ""

    def public(self) -> dict[str, Any]:
        """What anyone may see: used for branding before and after login."""
        return {
            "slug": self.slug,
            "name": self.name,
            "vertical": self.vertical,
            "branding": self.branding,
        }


_ORG_COLUMNS = "id, slug, name, vertical, timezone, branding, business_type"


async def get_org_by_slug(conn: AsyncConnection, slug: str) -> Org | None:
    row = (
        await conn.execute(text(f"SELECT {_ORG_COLUMNS} FROM orgs WHERE slug = :s"), {"s": slug})
    ).one_or_none()
    return Org(*row) if row else None


async def get_org_by_id(conn: AsyncConnection, org_id: UUID) -> Org | None:
    row = (
        await conn.execute(text(f"SELECT {_ORG_COLUMNS} FROM orgs WHERE id = :i"), {"i": org_id})
    ).one_or_none()
    return Org(*row) if row else None
