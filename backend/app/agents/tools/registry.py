"""Which tools each vertical gets. Keyed on org.vertical only, never on a slug (invariant 11).

Admin tools get their own registry, used only by the admin analytics chat: read-only
metric tools and nothing else (no proposals, no confirm, no knowledge search).
"""

from app.agents.tools.admin import admin_tools_for_vertical
from app.agents.tools.base import Tool
from app.agents.tools.booking import BOOKING_TOOLS
from app.agents.tools.common.confirm_proposal import confirm_proposal
from app.agents.tools.common.search_knowledge import search_knowledge
from app.agents.tools.store import STORE_TOOLS

COMMON: list[Tool] = [search_knowledge]

REGISTRY: dict[str, list[Tool]] = {
    "store": [*COMMON, *STORE_TOOLS, confirm_proposal],
    "booking": [*COMMON, *BOOKING_TOOLS, confirm_proposal],
}


def tools_for(vertical: str) -> dict[str, Tool]:
    """The vertical's tools by name. An unknown vertical is a KeyError: fail loudly."""
    return {t.name: t for t in REGISTRY[vertical]}


ADMIN_REGISTRY: dict[str, list[Tool]] = {v: admin_tools_for_vertical(v) for v in REGISTRY}


def admin_tools_for(vertical: str) -> dict[str, Tool]:
    """The admin analytics tools for the vertical. Unknown vertical: KeyError, as above."""
    return {t.name: t for t in ADMIN_REGISTRY[vertical]}
