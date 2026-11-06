"""Which tools each vertical gets. Keyed on org.vertical only, never on a slug (invariant 11).

Admin tools get their own registry, used only for admin analytics.
"""

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
