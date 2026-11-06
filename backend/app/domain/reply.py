"""Reply: what a domain operation hands back to whoever called it (a tool, or the server's
confirm-button path). Text for the model, cards for the UI. Kept free of agent imports."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Reply:
    text: str  # for the model; starts with a status word (PROPOSED, REFUSED, NOT FOUND, ...)
    cards: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True  # False: the call itself was wrong (bad SKU, nothing to change)
    status: str = "ok"  # machine-readable outcome, e.g. committed | refused | changed | not_found
