"""Text a customer wrote, on its way to the model: marked as data, unable to break out.

A delivery address or a saved memory is stored now and shown to the model later, in a
new turn or conversation. wrap() makes it inert there: control characters, '<' and '>'
are removed (so it can't close its own delimiter or open another), whitespace collapses
to one line, the length is capped, and the result sits inside
<user_data field="...">...</user_data>, which the system prompt declares untrusted.
"""

import re

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
DEFAULT_CAP = 200


def clean(value: str, cap: int = DEFAULT_CAP) -> str:
    """One line, no markup or control characters, at most `cap` characters."""
    text = re.sub(r"\s+", " ", re.sub(r"[<>]", "", _CONTROL.sub("", value))).strip()
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def wrap(field: str, value: str, cap: int = DEFAULT_CAP) -> str:
    return f'<user_data field="{field}">{clean(value, cap)}</user_data>'
