"""A small, hand-dated analytics fixture: every expected number in test_metrics.py is
worked out by hand from the rows below. "Now" is Thu 12 Nov 2026, 10:00 NPT (+05:45).

Store (tests.factories.SHOP_PRODUCTS prices: GRN-1 800, GRN-2 900, GRN-3 1500, BLK-3 650):
  o1  x  today 08:00          GRN-1 x2           1600  pending    conv c1
  o2  y  today 09:30          GRN-2 x1, BLK-3 x2 2200  confirmed  conv c2
  o3  x  today 09:00          GRN-3 x1           1500  cancelled  (no conversation)
  o4  y  yesterday 23:50      GRN-1 x1            800  delivered  conv c4
  o5  x  today 00:10          BLK-3 x1            650  shipped    conv c_old (started 1 Nov)
  o6  y  Mon 2 Nov 12:00      GRN-1 x3           2400  delivered
  o7  x  Sat 31 Oct 23:50     GRN-2 x1            900  delivered  (last month)
  conversations (customer): c1 x today 07:55, c2 y today 09:25, c3 x today 09:40 (no order),
    c4 y yesterday 23:45, c_old x Sun 1 Nov 08:00; plus one admin conversation today.
  stock_events: today x BLK-2 2/0, y BLK-2 1/0, x BLK-1 3/1; yesterday y BLK-1 2/1.

Studio (tests.factories.STUDIO_*: Mon–Thu 09–19, Fri 09–15, Sat closed, Sun 09–19, Wed
11 Nov holiday; swedish 60/3500, deep 90/5200, yoga 45/2200):
  b1 x asha swedish Thu 12 09:00 completed      b2 y bina yoga    Thu 12 09:15 completed
  b3 x asha deep    Thu 12 14:00 confirmed (c1) b4 y bina swedish Thu 12 16:00 cancelled fee 1750
  b5 x bina swedish Tue 10 10:00 completed      b6 y asha swedish Tue 10 11:00 cancelled fee 0
  b7 x bina yoga    Mon  9 09:00 completed      b8 y asha deep    Sun  8 12:00 completed (last week)
  b9 x bina deep    Fri 13 09:00 confirmed
  conversations: c1 x today 08:30 (b3), c2 y today 09:00 (none).

Both: every pre-existing user and conversation is moved to 1 Jan 2026. New customers:
z1 today 08:00, z2 Mon 9 Nov 12:00, z3 Sun 8 Nov 23:50; plus an admin created today.
Messages (role user): store c1 x 07:55, c2 y 09:25, c4 y yesterday 23:45, c_old x today
00:05, admin chat today; assistant messages never count. agent_runs today: 2 x
gemini-3.5-flash-lite (1000 in / 200 out each), 1 x gemini-3.1-flash-lite (3000 / 100),
1 x unknown-model (500 / 50); yesterday: 1 x gemini-3.5-flash-lite (9999 / 9999).
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.factories import Shop, Studio, insert_shop_order, insert_user, set_owner_tenant

NPT = ZoneInfo("Asia/Kathmandu")
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=NPT)
LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


def at(month: int, day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, month, day, hh, mm, tzinfo=NPT)


@dataclass
class Built:
    conversations: dict[str, uuid.UUID] = field(default_factory=dict)
    orders: dict[str, int] = field(default_factory=dict)


async def _conversation(
    conn: AsyncConnection, org: uuid.UUID, user: uuid.UUID, when: datetime, kind: str = "customer"
) -> uuid.UUID:
    return await conn.scalar(
        text(
            "INSERT INTO conversations (org_id, user_id, kind, created_at, updated_at) VALUES (:o, :u, :k, :t, :t) RETURNING id"
        ),
        {"o": org, "u": user, "k": kind, "t": when},
    )


async def _message(
    conn: AsyncConnection, org: uuid.UUID, user: uuid.UUID, conv: uuid.UUID, when: datetime, role: str = "user"
) -> None:
    await conn.execute(
        text(
            "INSERT INTO messages (org_id, user_id, conversation_id, role, content, created_at) VALUES (:o, :u, :c, :r, 'hi', :t)"
        ),
        {"o": org, "u": user, "c": conv, "r": role, "t": when},
    )


async def _run(
    conn: AsyncConnection,
    org: uuid.UUID,
    user: uuid.UUID,
    conv: uuid.UUID,
    model: str,
    tin: int,
    tout: int,
    when: datetime,
) -> None:
    await conn.execute(
        text("INSERT INTO agent_runs (org_id, user_id, conversation_id, provider, model, input_tokens, output_tokens, status, created_at) "
             "VALUES (:o, :u, :c, 'gemini', :m, :i, :out, 'ok', :t)"),
        {"o": org, "u": user, "c": conv, "m": model, "i": tin, "out": tout, "t": when},
    )  # fmt: skip


async def _common(conn: AsyncConnection, org: uuid.UUID, admin: uuid.UUID) -> None:
    """Old users/conversations out of every window; the new sign-ups; an admin today."""
    await conn.execute(text("UPDATE users SET created_at = :t WHERE org_id = :o"), {"t": LONG_AGO, "o": org})
    await conn.execute(
        text("UPDATE conversations SET created_at = :t, updated_at = :t WHERE org_id = :o"), {"t": LONG_AGO, "o": org}
    )
    for when, role in ((at(11, 12, 8), "customer"), (at(11, 9, 12), "customer"), (at(11, 8, 23, 50), "customer"),
                       (at(11, 12, 9), "admin")):  # fmt: skip
        uid = await insert_user(conn, org, role)
        await conn.execute(text("UPDATE users SET created_at = :t WHERE id = :u"), {"t": when, "u": uid})
    admin_conv = await _conversation(conn, org, admin, at(11, 12, 9, 50), "admin")
    await _message(conn, org, admin, admin_conv, at(11, 12, 9, 50))
    await _run(conn, org, admin, admin_conv, "gemini-3.5-flash-lite", 1000, 200, at(11, 12, 9, 50))


async def build_shop(owner: AsyncEngine, shop: Shop) -> Built:
    b = Built()
    x, y = shop.x, shop.y
    async with owner.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        await _common(conn, shop.org_id, shop.admin)
        for key, user, when in (("c1", x, at(11, 12, 7, 55)), ("c2", y, at(11, 12, 9, 25)), ("c3", x, at(11, 12, 9, 40)),
                                ("c4", y, at(11, 11, 23, 45)), ("c_old", x, at(11, 1, 8))):  # fmt: skip
            b.conversations[key] = await _conversation(conn, shop.org_id, user, when)
            await _message(conn, shop.org_id, user, b.conversations[key], when)
            await _message(conn, shop.org_id, user, b.conversations[key], when, role="assistant")
        await _message(conn, shop.org_id, x, b.conversations["c_old"], at(11, 12, 0, 5))  # x is back today
        await _run(conn, shop.org_id, x, b.conversations["c1"], "gemini-3.5-flash-lite", 1000, 200, at(11, 12, 7, 56))
        await _run(conn, shop.org_id, y, b.conversations["c2"], "gemini-3.1-flash-lite", 3000, 100, at(11, 12, 9, 26))
        await _run(conn, shop.org_id, x, b.conversations["c3"], "unknown-model", 500, 50, at(11, 12, 9, 41))
        await _run(conn, shop.org_id, y, b.conversations["c4"], "gemini-3.5-flash-lite", 9999, 9999, at(11, 11, 23, 46))
        for user, sku, req, avail, when in ((x, "BLK-2", 2, 0, at(11, 12, 9)), (y, "BLK-2", 1, 0, at(11, 12, 9, 10)),
                                            (x, "BLK-1", 3, 1, at(11, 12, 8, 30)), (y, "BLK-1", 2, 1, at(11, 11, 15))):  # fmt: skip
            await conn.execute(
                text("INSERT INTO stock_events (org_id, user_id, sku, requested_qty, available_qty, created_at) "
                     "VALUES (:o, :u, :s, :r, :a, :t)"),
                {"o": shop.org_id, "u": user, "s": sku, "r": req, "a": avail, "t": when},
            )  # fmt: skip
    orders = (
        ("o1", x, [("GRN-1", 2)], "pending", at(11, 12, 8), "c1"),
        ("o2", y, [("GRN-2", 1), ("BLK-3", 2)], "confirmed", at(11, 12, 9, 30), "c2"),
        ("o3", x, [("GRN-3", 1)], "cancelled", at(11, 12, 9), None),
        ("o4", y, [("GRN-1", 1)], "delivered", at(11, 11, 23, 50), "c4"),
        ("o5", x, [("BLK-3", 1)], "shipped", at(11, 12, 0, 10), "c_old"),
        ("o6", y, [("GRN-1", 3)], "delivered", at(11, 2, 12), None),
        ("o7", x, [("GRN-2", 1)], "delivered", at(10, 31, 23, 50), None),
    )
    for key, user, items, status, when, conv in orders:
        b.orders[key] = await insert_shop_order(owner, shop, user, items, status=status, created_at=when)
        if conv:
            async with owner.begin() as conn:
                await set_owner_tenant(conn, shop.org_id)
                await conn.execute(text("UPDATE orders SET conversation_id = :c WHERE org_id = :o AND order_number = :n"),
                                   {"c": b.conversations[conv], "o": shop.org_id, "n": b.orders[key]})  # fmt: skip
    return b


async def build_studio(owner: AsyncEngine, studio: Studio) -> Built:
    b = Built()
    x, y = studio.x, studio.y
    s, p = studio.services, studio.practitioners
    minutes = {"swedish": 60, "deep": 90, "yoga": 45}
    price = {"swedish": Decimal("3500"), "deep": Decimal("5200"), "yoga": Decimal("2200")}
    async with owner.begin() as conn:
        await set_owner_tenant(conn, studio.org_id)
        await _common(conn, studio.org_id, studio.admin)
        for key, user, when in (("c1", x, at(11, 12, 8, 30)), ("c2", y, at(11, 12, 9))):
            b.conversations[key] = await _conversation(conn, studio.org_id, user, when)
            await _message(conn, studio.org_id, user, b.conversations[key], when)
        bookings = (
            (x, "asha", "swedish", at(11, 12, 9), "completed", None, None),
            (y, "bina", "yoga", at(11, 12, 9, 15), "completed", None, None),
            (x, "asha", "deep", at(11, 12, 14), "confirmed", None, "c1"),
            (y, "bina", "swedish", at(11, 12, 16), "cancelled", Decimal("1750"), None),
            (x, "bina", "swedish", at(11, 10, 10), "completed", None, None),
            (y, "asha", "swedish", at(11, 10, 11), "cancelled", Decimal("0"), None),
            (x, "bina", "yoga", at(11, 9, 9), "completed", None, None),
            (y, "asha", "deep", at(11, 8, 12), "completed", None, None),
            (x, "bina", "deep", at(11, 13, 9), "confirmed", None, None),
        )
        for user, who, svc, start, status, fee, conv in bookings:
            await conn.execute(
                text("INSERT INTO bookings (org_id, user_id, service_id, practitioner_id, starts_at, ends_at, status, price, "
                     "cancellation_fee, conversation_id) VALUES (:o, :u, :s, :p, :a, :e, :st, :pr, :f, :c)"),
                {"o": studio.org_id, "u": user, "s": s[svc], "p": p[who], "a": start, "e": start + timedelta(minutes=minutes[svc]), "st": status,
                 "pr": price[svc], "f": fee, "c": b.conversations.get(conv) if conv else None},
            )  # fmt: skip
    return b
