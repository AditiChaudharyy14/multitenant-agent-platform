"""Org config: seed/<slug>/org.yaml loads, and app code never names a slug."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.db.session import app_engine
from app.tenancy.orgs import OrgConfig, check_orgs_present, load_org_configs

APP_DIR = Path(__file__).resolve().parents[2] / "app"


def test_seed_configs_cover_every_vertical(org_configs: list[OrgConfig]) -> None:
    # Several orgs may share a vertical (a new org is config + seed only); each vertical
    # still needs at least one org so its behaviour is exercised.
    assert {c.vertical for c in org_configs} == {"booking", "store"}
    assert len({c.slug for c in org_configs}) == len(org_configs)
    for c in org_configs:
        assert c.timezone == "Asia/Kathmandu"
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", c.branding.primary_color)
        assert c.branding.agent_name and c.branding.greeting and c.branding.logo_emoji


def test_no_app_code_names_a_slug(org_configs: list[OrgConfig]) -> None:
    """Invariant 11: behaviour is keyed on vertical. A slug literal in app/ is a smell."""
    offenders = [
        f"{path.relative_to(APP_DIR)}: {c.slug}"
        for path in APP_DIR.rglob("*.py")
        for c in org_configs
        if c.slug in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_invalid_config_rejected() -> None:
    good = {
        "slug": "x", "name": "X", "vertical": "store", "timezone": "Asia/Kathmandu",
        "order_prefix": "XX",
        "branding": {"primary_color": "#000000", "secondary_color": "#ffffff",
                     "logo_emoji": "x", "agent_name": "A", "greeting": "hi"},
    }  # fmt: skip
    OrgConfig.model_validate(good)
    bad_cases = (
        {"vertical": "bank"}, {"timezone": "Mars/Base"}, {"slug": "Bad Slug"},
        {"order_prefix": "ht"}, {"order_prefix": "TOOLONG"}, {"order_prefix": None},  # a store needs one
        {"currency": "rupees"},
    )  # fmt: skip
    for bad in bad_cases:
        with pytest.raises(ValidationError):
            OrgConfig.model_validate({**good, **bad})
    # A booking org has no orders, so it needs no order prefix; it needs a booking prefix.
    OrgConfig.model_validate({**good, "vertical": "booking", "order_prefix": None, "booking_prefix": "LW"})
    for bad in ({"booking_prefix": None}, {"booking_prefix": "lw"}, {"booking_prefix": "TOOLONG"}):
        with pytest.raises(ValidationError):
            OrgConfig.model_validate({**good, "vertical": "booking", "order_prefix": None, **bad})


def test_folder_must_match_slug(tmp_path: Path) -> None:
    (tmp_path / "wrong").mkdir()
    (tmp_path / "wrong" / "org.yaml").write_text(
        "slug: right\nname: R\nvertical: store\ntimezone: UTC\norder_prefix: RT\nbranding:\n"
        "  primary_color: '#000000'\n  secondary_color: '#000000'\n  logo_emoji: x\n"
        "  agent_name: a\n  greeting: g\n"
    )
    with pytest.raises(RuntimeError, match="does not match folder"):
        load_org_configs(tmp_path)


async def test_startup_check_fails_for_unsynced_org(org_configs: list[OrgConfig]) -> None:
    ghost = org_configs[0].model_copy(update={"slug": "never-synced-org"})
    async with app_engine.connect() as conn:  # app_user: read-only check
        with pytest.raises(RuntimeError, match="never-synced-org.*make sync-orgs"):
            await check_orgs_present(conn, [ghost])


async def test_startup_check_passes_after_sync(synced_orgs: dict[str, OrgConfig]) -> None:
    async with app_engine.connect() as conn:
        await check_orgs_present(conn, list(synced_orgs.values()))
        branding = await conn.scalar(
            text("SELECT branding FROM orgs WHERE slug = :s"), {"s": synced_orgs["store"].slug}
        )
    assert branding == synced_orgs["store"].branding.model_dump()
