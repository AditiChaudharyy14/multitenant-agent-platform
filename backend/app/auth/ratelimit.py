"""In-process sliding-window rate limiter.

Per process only: fine for this single-instance deployment. Several replicas would need
a shared store. Time comes from app.clock so tests can move it.
"""

import math
from collections import deque
from datetime import datetime, timedelta

from app import clock


class SlidingWindowLimiter:
    def __init__(self, limit: int, window: timedelta) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[datetime]] = {}

    def hit(self, key: str) -> int | None:
        """Record an attempt. Returns None if allowed, else seconds until the next slot."""
        now = clock.now()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:  # drop attempts that left the window
            hits.popleft()
        if len(hits) >= self.limit:
            # A refused attempt isn't recorded, so hammering doesn't extend the lockout.
            return max(1, math.ceil((hits[0] + self.window - now).total_seconds()))
        hits.append(now)
        return None

    def reset(self) -> None:
        self._hits.clear()


# 10 login attempts per minute per (client IP, email).
login_limiter = SlidingWindowLimiter(limit=10, window=timedelta(minutes=1))
