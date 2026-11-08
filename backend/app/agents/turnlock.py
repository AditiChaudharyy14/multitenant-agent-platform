"""One reply at a time per conversation: a Postgres advisory lock held for the whole turn.

acquire() takes pg_try_advisory_lock on a dedicated pooled connection, keyed on the
conversation id; None means another reply is still being written (the API answers 409).
The turn task releases it after persisting its answer, before it says `done`, so the
customer's next message never races the lock. The lock is session-level, so release()
unlocks explicitly before the connection goes back to the pool, and throws the connection
away if that fails: a pooled connection must never carry a lock into another request.
"""

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import session as db

log = logging.getLogger("app.agent")

_KEY = "hashtextextended(CAST(:c AS text), 0)"


class TurnLock:
    def __init__(self, conn: AsyncConnection, conversation_id: UUID) -> None:
        self._conn: AsyncConnection | None = conn
        self.conversation_id = conversation_id

    async def release(self) -> None:
        """Idempotent; never raises (it runs while a turn is finishing)."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            await conn.execute(text(f"SELECT pg_advisory_unlock({_KEY})"), {"c": str(self.conversation_id)})
            await conn.commit()
            await conn.close()
        except Exception:
            log.exception("could not release the turn lock of %s; discarding the connection", self.conversation_id)
            await conn.invalidate()


async def acquire(conversation_id: UUID) -> TurnLock | None:
    conn = await db.app_engine.connect()
    try:
        got = await conn.scalar(text(f"SELECT pg_try_advisory_lock({_KEY})"), {"c": str(conversation_id)})
        await conn.commit()  # end the implicit transaction; the session-level lock stays
    except Exception:
        await conn.invalidate()
        raise
    if not got:
        await conn.close()
        return None
    return TurnLock(conn, conversation_id)
