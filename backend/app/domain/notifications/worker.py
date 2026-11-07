"""Background loops in the backend process: the sender and the reminder scan.

Started from the app lifespan only when EMAIL_WORKER_ENABLED is set. Each loop survives
its own errors (logged, then retried on the next tick) and stops when cancelled.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.config import get_settings
from app.domain.notifications import reminders, sender

log = logging.getLogger("app.email")


async def _every(seconds: int, job: Callable[[], Awaitable[int]], name: str) -> None:
    while True:
        try:
            if n := await job():
                log.info("%s: %d", name, n)
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not kill the loop
            log.exception("%s failed; retrying in %ds", name, seconds)
        await asyncio.sleep(seconds)


def start() -> list[asyncio.Task[None]]:
    s = get_settings()
    return [
        asyncio.create_task(_every(s.email_poll_seconds, sender.run_once, "emails sent"), name="email-sender"),
        asyncio.create_task(_every(s.reminder_interval_seconds, reminders.run_once, "reminders queued"), name="reminders"),
    ]
