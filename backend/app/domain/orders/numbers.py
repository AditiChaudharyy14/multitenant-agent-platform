"""Human-friendly order numbers: PREFIX-NNNN (e.g. HT-1042). UUIDs never reach customers.

The prefix comes from org config. Parsing is lenient about form ("ht 1042", "#1042",
"1042") but strict about the prefix: another org's prefix is simply not found here.
"""

import re

_NUMBER = re.compile(r"^\s*(?:order\s+)?#?\s*(?:(?P<prefix>[A-Za-z]{2,4})[\s-]*)?(?P<n>\d{1,9})\s*$", re.IGNORECASE)


def parse_order_number(raw: str, prefix: str) -> int | None:
    """The numeric part if `raw` names an order of this org's prefix, else None."""
    m = _NUMBER.match(raw or "")
    if not m:
        return None
    if m["prefix"] and m["prefix"].upper() != prefix.upper():
        return None
    n = int(m["n"])
    return n if n > 0 else None


def display_number(prefix: str, n: int) -> str:
    return f"{prefix}-{n}"
