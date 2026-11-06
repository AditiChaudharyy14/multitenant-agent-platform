"""Human-friendly record numbers: PREFIX-NNNN (order HT-1042, booking LW-1001). UUIDs never
reach customers.

The prefix comes from org config. Parsing is lenient about form ("lw 1001", "#1001",
"booking 1001", "1001") but strict about the prefix: another org's prefix is simply not
found here.
"""

import re

_NUMBER = re.compile(
    r"^\s*(?:(?:order|booking|appointment)\s+)?#?\s*(?:(?P<prefix>[A-Za-z]{2,4})[\s-]*)?(?P<n>\d{1,9})\s*$",
    re.IGNORECASE,
)


def parse_number(raw: str, prefix: str) -> int | None:
    """The numeric part if `raw` names a record with this org's prefix, else None."""
    m = _NUMBER.match(raw or "")
    if not m:
        return None
    if m["prefix"] and m["prefix"].upper() != prefix.upper():
        return None
    n = int(m["n"])
    return n if n > 0 else None


def display_number(prefix: str | None, n: int) -> str:
    return f"{prefix}-{n}"
