"""Times are stored as UTC timestamptz and shown in the org's timezone."""

from datetime import datetime
from zoneinfo import ZoneInfo


def local_display(at: datetime, timezone: str) -> str:
    """'Sun 1 Nov 2026, 10:00' in the org's timezone."""
    local = at.astimezone(ZoneInfo(timezone))
    return f"{local:%a} {local.day} {local:%b %Y, %H:%M}"
