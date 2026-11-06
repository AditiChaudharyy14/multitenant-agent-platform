"""Server-side citation check: the model may cite only sources it was given this turn.

A citation is [doc_id#section]; several may share brackets: [faq#a, faq#b]. Bracketed text
that isn't made only of citation ids (markdown link text, [sic]) is left alone.
"""

import re

from app.agents.tools.base import Citation

_GROUP = re.compile(r"(\s*)\[([^\[\]\n]{1,300})\]")  # leading space, so a dropped one leaves no gap
_ID = re.compile(r"^[A-Za-z0-9][\w.-]*#[\w.-]+$")
_SPLIT = re.compile(r"\s*[,;]\s*")


def filter_citations(
    text: str, allowed: dict[str, Citation]
) -> tuple[str, list[Citation], list[str]]:
    """(text without invented citations, kept citations in order, dropped ids in order)."""
    kept: dict[str, Citation] = {}
    dropped: dict[str, None] = {}  # dicts as ordered sets

    def replace(m: re.Match[str]) -> str:
        ids = _SPLIT.split(m.group(2).strip())
        if not all(_ID.match(i) for i in ids):
            return m.group(0)  # not a citation group
        good = [i for i in ids if i in allowed]
        for i in ids:
            if i in allowed:
                kept.setdefault(i, allowed[i])
            else:
                dropped.setdefault(i)
        return m.group(1) + "".join(f"[{i}]" for i in good) if good else ""

    return _GROUP.sub(replace, text), list(kept.values()), list(dropped)
