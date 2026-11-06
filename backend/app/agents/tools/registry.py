"""Which tools each vertical gets. Keyed on org.vertical only, never on a slug (invariant 11).

Store and booking tools join their lists in later milestones; admin tools get their own
registry, used only for admin analytics.
"""

from app.agents.tools.base import Tool
from app.agents.tools.common.search_knowledge import search_knowledge

COMMON: list[Tool] = [search_knowledge]

REGISTRY: dict[str, list[Tool]] = {
    "store": [*COMMON],
    "booking": [*COMMON],
}


def tools_for(vertical: str) -> dict[str, Tool]:
    """The vertical's tools by name. An unknown vertical is a KeyError: fail loudly."""
    return {t.name: t for t in REGISTRY[vertical]}
