"""Per-model requests-per-minute limiter (sliding one-minute window), process-global.

No lock: asyncio is single-threaded and nothing awaits between checking the window and
recording the call, so two coroutines can't both take the last slot.
"""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

WINDOW = 60.0  # seconds


class RpmLimiter:
    def __init__(
        self,
        rpm: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.rpm = rpm
        self._clock, self._sleep = clock, sleep
        self._calls: deque[float] = deque()  # start times inside the current window

    async def acquire(self) -> None:
        while True:
            now = self._clock()
            while self._calls and now - self._calls[0] >= WINDOW:
                self._calls.popleft()  # older than a minute: no longer counts
            if len(self._calls) < self.rpm:
                self._calls.append(now)
                return
            # Wait until the oldest call leaves the window, then re-check.
            await self._sleep(self._calls[0] + WINDOW - now)


_registry: dict[tuple[str, str], RpmLimiter] = {}


def limiter_for(provider: str, model: str, rpm: int) -> RpmLimiter:
    """One limiter per (provider, model) per process, shared by every request."""
    key = (provider, model)
    if key not in _registry or _registry[key].rpm != rpm:
        _registry[key] = RpmLimiter(rpm)
    return _registry[key]
