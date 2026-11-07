"""Which tools each vertical gets. Keyed on org.vertical only, never on a slug (invariant 11).

Optional features (org.yaml `features`, see tenancy.orgs.Features) add tools on top; they
are on by default, and an org can switch one off in config alone.

Admin tools get their own registry, used only by the admin analytics chat: read-only
metric tools and nothing else (no proposals, no confirm, no knowledge search, no memory).
"""

from collections.abc import Mapping

from app.agents.tools.admin import admin_tools_for_vertical
from app.agents.tools.base import Tool
from app.agents.tools.booking import BOOKING_TOOLS
from app.agents.tools.common.confirm_proposal import confirm_proposal
from app.agents.tools.common.memory import MEMORY_TOOLS
from app.agents.tools.common.search_knowledge import search_knowledge
from app.agents.tools.store import STORE_TOOLS
from app.tenancy.orgs import Features

COMMON: list[Tool] = [search_knowledge]

# feature name -> the tools it adds (to every vertical).
FEATURE_TOOLS: dict[str, list[Tool]] = {"memory": MEMORY_TOOLS}

# Everything a vertical can have, features included (the schema tests walk all of it).
REGISTRY: dict[str, list[Tool]] = {
    "store": [*COMMON, *STORE_TOOLS, confirm_proposal, *MEMORY_TOOLS],
    "booking": [*COMMON, *BOOKING_TOOLS, confirm_proposal, *MEMORY_TOOLS],
}


def tools_for(vertical: str, features: Mapping[str, bool] | None = None) -> dict[str, Tool]:
    """The vertical's tools by name, minus those of any feature switched off. A feature
    missing from `features` takes its default. Unknown vertical: KeyError, fail loudly."""
    flags = {name: field.default for name, field in Features.model_fields.items()} | dict(features or {})
    off = {t.name for name, tools in FEATURE_TOOLS.items() if not flags[name] for t in tools}
    return {t.name: t for t in REGISTRY[vertical] if t.name not in off}


ADMIN_REGISTRY: dict[str, list[Tool]] = {v: admin_tools_for_vertical(v) for v in REGISTRY}


def admin_tools_for(vertical: str) -> dict[str, Tool]:
    """The admin analytics tools for the vertical. Unknown vertical: KeyError, as above."""
    return {t.name: t for t in ADMIN_REGISTRY[vertical]}
