"""SlotProvider: the interface the booking code uses to ask "what is free?" and "is this
start bookable?". LocalSlotProvider answers from our own Postgres tables using the pure
rules in engine.py. A hosted scheduler (Cal.com, say) could implement the same three
methods; nothing in app/domain/bookings would change. Commits always go through our own
bookings table, whose EXCLUDE constraints are the final word on conflicts either way.

Customers can't see other customers' bookings (RLS), so practitioner busy time comes from
the SECURITY DEFINER function busy_intervals(), which returns only (practitioner_id,
starts_at, ends_at) for the caller's org. The customer's own bookings are read directly.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.slots import engine
from app.domain.slots.model import SEARCH_DAYS, Ignore, Interval, Schedule, Service, Slot
from app.tenancy.context import ToolContext


class SlotProvider(Protocol):
    async def find(
        self, conn: AsyncConnection, ctx: ToolContext, service: Service, days: Iterable[date], now: datetime, *,
        practitioner: uuid.UUID | None = None, window: tuple[time, time] | None = None, ignore: Ignore | None = None,
    ) -> list[Slot]: ...  # fmt: skip

    async def check(
        self, conn: AsyncConnection, ctx: ToolContext, service: Service, start: datetime, practitioner: uuid.UUID,
        now: datetime, *, ignore: Ignore | None = None,
    ) -> str | None: ...  # fmt: skip

    async def nearest_alternatives(
        self, conn: AsyncConnection, ctx: ToolContext, service: Service, target: datetime, now: datetime, *,
        practitioner: uuid.UUID | None = None, ignore: Ignore | None = None, k: int = 3,
    ) -> list[Slot]: ...  # fmt: skip

    async def schedule(self, conn: AsyncConnection) -> Schedule: ...


class LocalSlotProvider:
    """Reads opening hours, holidays and busy time from Postgres; decides with engine.py."""

    def __init__(self, timezone: str) -> None:
        self.timezone = timezone

    async def schedule(self, conn: AsyncConnection) -> Schedule:
        hours = {
            wd: (opens, closes)
            for wd, opens, closes in (await conn.execute(text("SELECT weekday, opens, closes FROM opening_hours"))).all()
        }
        holidays = dict((await conn.execute(text("SELECT day, name FROM holidays"))).all())
        return Schedule(self.timezone, hours, frozenset(holidays), holidays)

    async def _busy(
        self, conn: AsyncConnection, ctx: ToolContext, start: datetime, end: datetime, ignore: Ignore | None,
    ) -> tuple[dict[uuid.UUID, list[Interval]], list[Interval]]:  # fmt: skip
        """(practitioner busy time, the customer's own busy time) overlapping [start, end)."""
        practitioners: dict[uuid.UUID, list[Interval]] = defaultdict(list)
        rows = await conn.execute(text("SELECT practitioner_id, starts_at, ends_at FROM busy_intervals(:a, :b)"),
                                  {"a": start, "b": end})  # fmt: skip
        for pid, s, e in rows.all():
            if ignore and (pid, s, e) == (ignore.practitioner_id, ignore.start, ignore.end):
                continue  # the booking being moved doesn't block its own new time
            practitioners[pid].append(Interval(s, e))
        mine = await conn.execute(
            text(
                "SELECT starts_at, ends_at FROM bookings WHERE org_id = :o AND user_id = :u AND status = 'confirmed' "
                "AND tstzrange(starts_at, ends_at, '[)') && tstzrange(:a, :b, '[)')"
            ),
            {"o": ctx.org_id, "u": ctx.user_id, "a": start, "b": end},
        )
        customer = [
            Interval(s, e) for s, e in mine.all() if not (ignore and (s, e) == (ignore.start, ignore.end))
        ]
        return practitioners, customer

    def _span(self, days: Sequence[date]) -> tuple[datetime, datetime]:
        tz = ZoneInfo(self.timezone)
        first = datetime.combine(min(days), time(0), tzinfo=tz)
        last = datetime.combine(max(days) + timedelta(days=1), time(0), tzinfo=tz)
        return first.astimezone(UTC), last.astimezone(UTC)

    async def find(
        self, conn: AsyncConnection, ctx: ToolContext, service: Service, days: Iterable[date], now: datetime, *,
        practitioner: uuid.UUID | None = None, window: tuple[time, time] | None = None, ignore: Ignore | None = None,
    ) -> list[Slot]:  # fmt: skip
        days = sorted(set(days))
        if not days:
            return []
        schedule = await self.schedule(conn)
        busy, customer = await self._busy(conn, ctx, *self._span(days), ignore)
        who = {pid: name for pid, name in service.practitioners.items() if practitioner in (None, pid)}
        return engine.free_slots(schedule, service.duration_min, days, who, busy, customer, now, window)

    async def check(
        self, conn: AsyncConnection, ctx: ToolContext, service: Service, start: datetime, practitioner: uuid.UUID,
        now: datetime, *, ignore: Ignore | None = None,
    ) -> str | None:  # fmt: skip
        schedule = await self.schedule(conn)
        end = start + timedelta(minutes=service.duration_min)
        busy, customer = await self._busy(conn, ctx, start, end, ignore)
        return engine.check_slot(schedule, start, service.duration_min, now, busy.get(practitioner, []), customer)

    async def nearest_alternatives(
        self, conn: AsyncConnection, ctx: ToolContext, service: Service, target: datetime, now: datetime, *,
        practitioner: uuid.UUID | None = None, ignore: Ignore | None = None, k: int = 3,
    ) -> list[Slot]:  # fmt: skip
        """The k free slots closest to `target`: the given practitioner's first, then any
        qualified one. Looks from max(now, target − 14 days) to target + 14 days."""
        tz = ZoneInfo(self.timezone)
        first = max(now, target - timedelta(days=SEARCH_DAYS)).astimezone(tz).date()
        last = (target + timedelta(days=SEARCH_DAYS)).astimezone(tz).date()
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        slots = await self.find(conn, ctx, service, days, now, ignore=ignore)
        return engine.nearest(target, slots, practitioner, k)
