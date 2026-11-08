"""Setup steps and assertions, run as app_owner on the scenario's clone (org-scoped).

Nothing here trusts the model: expected values come from the scenario file (derived from
seed/*/data.yaml), actual values from the database.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app import clock
from scenario import Assertions, BookingCheck, DbCheck, OrderCheck, ReplyChecks, Scenario, SetupStep

# Tables no_new_rows / new_rows may name (all org-scoped).
COUNTED_TABLES = ("orders", "order_items", "order_status_history", "bookings", "proposals", "products",
                  "conversations", "stock_events", "customer_memory", "messages", "agent_runs")

NEXT_STATUS = {"pending": "confirmed", "confirmed": "shipped", "shipped": "delivered"}


@dataclass
class OrgInfo:
    org_id: uuid.UUID
    slug: str
    vertical: str
    timezone: str
    order_prefix: str
    booking_prefix: str


@dataclass
class TurnRecord:
    request: str  # what was sent (text, or the action)
    reply: str = ""
    status: str = ""  # done.status: complete | error | stopped
    tools: list[str] = field(default_factory=list)  # agent_runs.tools_called (server:… for buttons)
    cards: list[dict] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    turn_id: str | None = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    run_error: str | None = None  # agent_runs.error (cassette problems show up here)
    http_status: int = 200  # a turn the server refused outright (expect_http) keeps its status


def seed_row_id(org: OrgInfo, kind: str, key: str) -> uuid.UUID:
    """The seeder's own id scheme (app/seed/run.py _rid)."""
    return uuid.uuid5(org.org_id, f"{kind}:{key}")


async def user_id(conn: AsyncConnection, email: str) -> uuid.UUID:
    uid = await conn.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": email.lower()})
    if uid is None:
        raise ValueError(f"no seeded user {email!r} in this org")
    return uid


async def number_of(conn: AsyncConnection, org: OrgInfo, kind: str, key: str) -> str:
    rid = seed_row_id(org, kind, key)
    if kind == "order":
        n = await conn.scalar(text("SELECT order_number FROM orders WHERE id = :i"), {"i": rid})
        prefix = org.order_prefix
    else:
        n = await conn.scalar(text("SELECT booking_number FROM bookings WHERE id = :i"), {"i": rid})
        prefix = org.booking_prefix
    if n is None:
        raise ValueError(f"no seeded {kind} {key!r}")
    return f"{prefix}-{n}"


_PLACEHOLDER = re.compile(r"\{\{(order|booking):([\w-]+)\}\}")
_CHARS = re.compile(r"\{\{chars:(\d+)\}\}")
_FILLER = "Please tell me everything about your teas. "


def expand_chars(s: str) -> str:
    """{{chars:N}} → exactly N characters of plain filler text (for the message-length limit)."""
    return _CHARS.sub(lambda m: (_FILLER * (int(m.group(1)) // len(_FILLER) + 1))[: int(m.group(1))], s)


async def render(conn: AsyncConnection, org: OrgInfo, s: str) -> str:
    """Replace {{order:key}} / {{booking:key}} with the seeded row's number, {{chars:N}} with filler."""
    s = expand_chars(s)
    out, pos = [], 0
    for m in _PLACEHOLDER.finditer(s):
        out += [s[pos : m.start()], await number_of(conn, org, m.group(1), m.group(2))]
        pos = m.end()
    return "".join(out) + s[pos:]


_WINDOW = re.compile(r"\{\{(today|tomorrow|yesterday|week_start|next_week|month_start|next_month)\}\}")


def window_bounds(org: OrgInfo) -> dict[str, str]:
    """Local midnights around the frozen clock, worked out here independently of the app's
    window code (it is what's under test), as timestamptz literals for SQL."""
    tz = ZoneInfo(org.timezone)
    today = clock.now().astimezone(tz).date()
    week = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    days = {
        "today": today, "tomorrow": today + timedelta(days=1), "yesterday": today - timedelta(days=1),
        "week_start": week, "next_week": week + timedelta(days=7),
        "month_start": month, "next_month": (month + timedelta(days=32)).replace(day=1),
    }  # fmt: skip
    return {k: "TIMESTAMPTZ '" + datetime(d.year, d.month, d.day, tzinfo=tz).isoformat() + "'" for k, d in days.items()}


def render_sql(org: OrgInfo, sql: str) -> str:
    bounds = window_bounds(org)
    return _WINDOW.sub(lambda m: bounds[m.group(1)], sql)


def _number_forms(value: Any) -> list[str]:
    """How a ground-truth value may appear in a reply: 4450.00 → 4450, 4450.00, 4450.0."""
    if isinstance(value, bool) or value is None:
        return [str(value)]
    if isinstance(value, (int, float, Decimal)):
        d = Decimal(str(value))
        forms = {f"{d:.2f}", f"{d:.1f}"}
        if d == d.to_integral_value():
            forms.add(str(int(d)))
        return sorted(forms, key=len, reverse=True)
    return [str(value)]


def reply_has_value(reply: str, value: Any) -> bool:
    flat = re.sub(r"(?<=\d),(?=\d{3})", "", reply)  # 4,450.00 → 4450.00
    if not isinstance(value, (int, float, Decimal)):
        return str(value).lower() in flat.lower()
    return any(re.search(rf"(?<![\d.]){re.escape(f)}(?!\d|\.\d)", flat) for f in _number_forms(value))


def _local(org: OrgInfo, raw: str) -> datetime:
    return datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo(org.timezone))


# ───────────── setup ─────────────


async def apply_setup(conn: AsyncConnection, org: OrgInfo, step: SetupStep) -> None:
    if step.set_price:
        r = await conn.execute(text("UPDATE products SET price = :p WHERE sku = :s"),
                               {"p": Decimal(step.set_price.price), "s": step.set_price.sku})  # fmt: skip
        _one(r.rowcount, f"product {step.set_price.sku}")
    elif step.set_stock:
        r = await conn.execute(text("UPDATE products SET stock = :q WHERE sku = :s"),
                               {"q": step.set_stock.stock, "s": step.set_stock.sku})  # fmt: skip
        _one(r.rowcount, f"product {step.set_stock.sku}")
    elif step.advance_order:
        oid = seed_row_id(org, "order", step.advance_order.order)
        row = (await conn.execute(text("SELECT status, user_id FROM orders WHERE id = :i"), {"i": oid})).one()
        to = NEXT_STATUS[row.status]
        now = clock.now()
        await conn.execute(text("UPDATE orders SET status = :s, updated_at = :n WHERE id = :i"), {"s": to, "n": now, "i": oid})
        await conn.execute(
            text("INSERT INTO order_status_history (org_id, user_id, order_id, from_status, to_status, changed_at, changed_by) "
                 "VALUES (:o, :u, :i, :f, :t, :n, 'admin')"),
            {"o": org.org_id, "u": row.user_id, "i": oid, "f": row.status, "t": to, "n": now},
        )  # fmt: skip
    elif step.add_booking:
        b = step.add_booking
        svc = (await conn.execute(text("SELECT id, duration_min, price FROM services WHERE name = :n"), {"n": b.service})).one()
        pid = await conn.scalar(text("SELECT id FROM practitioners WHERE name = :n"), {"n": b.practitioner})
        if pid is None:
            raise ValueError(f"no practitioner {b.practitioner!r}")
        starts = _local(org, b.start)
        await conn.execute(
            text("INSERT INTO bookings (org_id, user_id, service_id, practitioner_id, starts_at, ends_at, status, price, created_at) "
                 "VALUES (:o, :u, :s, :p, :a, :e, 'confirmed', :price, :now)"),
            {"o": org.org_id, "u": await user_id(conn, b.customer), "s": svc.id, "p": pid, "a": starts,
             "e": starts + timedelta(minutes=svc.duration_min), "price": svc.price, "now": clock.now()},
        )  # fmt: skip
    elif step.sql:
        await conn.execute(text(render_sql(org, step.sql)))
    elif step.memory:
        await conn.execute(
            text("INSERT INTO customer_memory (org_id, user_id, key, value, source) VALUES (:o, :u, :k, :v, 'stated')"),
            {"o": org.org_id, "u": await user_id(conn, step.memory.customer), "k": step.memory.key, "v": step.memory.value},
        )  # fmt: skip
    elif step.analytics_history:
        from app.config import get_settings
        from app.seed.run import apply_analytics

        await apply_analytics(conn, get_settings().seed_dir, org.slug, org.org_id)


def _one(rowcount: int, what: str) -> None:
    if rowcount != 1:
        raise ValueError(f"setup: {what} not found")


async def table_counts(conn: AsyncConnection) -> dict[str, int]:
    return {t: await conn.scalar(text(f"SELECT count(*) FROM {t}")) for t in COUNTED_TABLES}  # noqa: S608


# ───────────── assertions ─────────────


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.DOTALL)


def check_reply(checks: ReplyChecks, reply: str, tools: list[str], where: str) -> list[str]:
    fails = []
    for p in checks.reply_matches:
        if not _rx(p).search(reply):
            fails.append(f"{where}: reply does not match /{p}/")
    for p in checks.reply_not_matches:
        if m := _rx(p).search(reply):
            fails.append(f"{where}: reply matches forbidden /{p}/ (found {m.group(0)!r})")
    if checks.tools_called:
        for t in checks.tools_called.includes:
            if t not in tools:
                fails.append(f"{where}: tool {t} was not called (called: {tools or 'none'})")
        for t in checks.tools_called.excludes:
            if t in tools:
                fails.append(f"{where}: tool {t} was called but must not be")
    return fails


async def check_all(conn: AsyncConnection, org: OrgInfo, sc: Scenario, me: uuid.UUID,
                    turns: list[TurnRecord], before: dict[str, int]) -> list[str]:  # fmt: skip
    a: Assertions = sc.assertions
    fails: list[str] = []
    if not turns:  # an expect_http scenario: nothing was said, only the state checks apply
        return await _state_checks(conn, org, me, a, "", before)
    for i, (turn, t) in enumerate(zip(sc.turns, turns, strict=True)):
        if turn.expect_http is not None and t.http_status != turn.expect_http:
            fails.append(f"turn {i + 1}: HTTP {t.http_status}, expected {turn.expect_http}")
        if turn.expect:
            fails += check_reply(await _render_checks(conn, org, turn.expect), t.reply, t.tools, f"turn {i + 1}")
    all_tools = [name for t in turns for name in t.tools]
    final = turns[-1].reply if turns else ""
    rendered = await _render_checks(conn, org, a)
    fails += check_reply(rendered, final, all_tools, "final")
    for p in rendered.any_reply_not_matches:
        for i, t in enumerate(turns):
            if m := _rx(p).search(t.reply):
                fails.append(f"turn {i + 1}: reply matches forbidden /{p}/ (found {m.group(0)!r})")
    cited = [c for t in turns for c in t.citations]
    for doc in a.citations_include:  # "shipping|faq": a citation from either document
        if not any(c == d or c.startswith(d + "#") for d in doc.split("|") for c in cited):
            fails.append(f"no citation from {doc} (cited: {cited or 'none'})")
    if a.no_citations and cited:
        fails.append(f"expected no citations, got {cited}")
    return fails + await _state_checks(conn, org, me, a, final, before)


async def _state_checks(conn: AsyncConnection, org: OrgInfo, me: uuid.UUID, a: Assertions, final: str,
                        before: dict[str, int]) -> list[str]:  # fmt: skip
    fails: list[str] = []
    after = await table_counts(conn)
    for table in a.no_new_rows:
        if after[table] > before[table]:
            fails.append(f"{table}: {after[table] - before[table]} new row(s), expected none")
    for table, n in a.new_rows.items():
        if after[table] - before[table] != n:
            fails.append(f"{table}: {after[table] - before[table]} new row(s), expected {n}")
    for check in a.db:
        fails += await _db_check(conn, org, me, check)
    for query in a.reply_contains_sql:
        want = await conn.scalar(text(render_sql(org, query)))
        if not reply_has_value(final, want):
            fails.append(f"final: reply lacks the DB value {want!r} of {query!r}")
    return fails


async def _render_checks(conn: AsyncConnection, org: OrgInfo, checks: Any) -> Any:
    """The same checks with {{order:..}} placeholders filled in their regexes."""
    data = checks.model_dump()
    for k in ("reply_matches", "reply_not_matches", "any_reply_not_matches"):
        if k in data:
            data[k] = [await render(conn, org, p) for p in data[k]]
    return type(checks).model_validate(data)


async def _who(conn: AsyncConnection, me: uuid.UUID, who: str) -> uuid.UUID:
    return me if who == "me" else await user_id(conn, who)


async def _db_check(conn: AsyncConnection, org: OrgInfo, me: uuid.UUID, check: DbCheck) -> list[str]:
    if check.stock is not None:
        fails = []
        for sku, want in check.stock.items():
            got = await conn.scalar(text("SELECT stock FROM products WHERE sku = :s"), {"s": sku})
            if got != want:
                fails.append(f"stock {sku}: {got}, expected {want}")
        return fails
    if check.proposals is not None:
        p = check.proposals
        where, params = ("WHERE status = :s", {"s": p.status}) if p.status else ("", {})
        got = await conn.scalar(text(f"SELECT count(*) FROM proposals {where}"), params)  # noqa: S608
        return [] if got == p.count else [f"proposals{f' ({p.status})' if p.status else ''}: {got}, expected {p.count}"]
    if check.sql is not None:
        got = await conn.scalar(text(render_sql(org, check.sql.query)))
        got = str(got) if isinstance(got, (Decimal, uuid.UUID, datetime)) else got
        return [] if got == check.sql.equals else [f"sql {check.sql.query!r}: {got!r}, expected {check.sql.equals!r}"]
    if check.order is not None:
        return await _order_check(conn, org, me, check.order)
    assert check.booking is not None
    return await _booking_check(conn, org, me, check.booking)


async def _order_check(conn: AsyncConnection, org: OrgInfo, me: uuid.UUID, c: OrderCheck) -> list[str]:
    if c.ref:
        oid, label = seed_row_id(org, "order", c.ref), f"order {c.ref}"
    else:
        uid = await _who(conn, me, c.latest_for or "me")
        oid = await conn.scalar(text("SELECT id FROM orders WHERE user_id = :u ORDER BY order_number DESC LIMIT 1"), {"u": uid})
        label = f"latest order of {c.latest_for}"
        if oid is None:
            return [f"{label}: there is none"]
    o = (await conn.execute(text("SELECT order_number, status, total FROM orders WHERE id = :i"), {"i": oid})).one_or_none()
    if o is None:
        return [f"{label}: not found"]
    label += f" ({org.order_prefix}-{o.order_number})"
    fails = []
    if c.status and o.status != c.status:
        fails.append(f"{label}: status {o.status}, expected {c.status}")
    if c.total and o.total != Decimal(c.total):
        fails.append(f"{label}: total {o.total}, expected {c.total}")
    lines = (await conn.execute(text("SELECT sku, qty, unit_price FROM order_items WHERE order_id = :i"), {"i": oid})).all()
    if c.items is not None:
        got = {r.sku: r.qty for r in lines}
        if got != c.items:
            fails.append(f"{label}: items {got}, expected {c.items}")
    for sku, price in (c.unit_prices or {}).items():
        got_p = next((r.unit_price for r in lines if r.sku == sku), None)
        if got_p != Decimal(price):
            fails.append(f"{label}: unit price of {sku} {got_p}, expected {price}")
    if c.history_to is not None:
        hist = (await conn.execute(
            text("SELECT to_status FROM order_status_history WHERE order_id = :i ORDER BY changed_at, to_status"), {"i": oid}
        )).scalars().all()  # fmt: skip
        if list(hist) != c.history_to:
            fails.append(f"{label}: history {list(hist)}, expected {c.history_to}")
    return fails


async def _booking_check(conn: AsyncConnection, org: OrgInfo, me: uuid.UUID, c: BookingCheck) -> list[str]:
    base = ("SELECT b.booking_number, b.status, b.starts_at, b.cancellation_fee, s.name AS service, p.name AS practitioner "
            "FROM bookings b JOIN services s ON s.id = b.service_id JOIN practitioners p ON p.id = b.practitioner_id ")  # fmt: skip
    if c.ref:
        row = (await conn.execute(text(base + "WHERE b.id = :i"), {"i": seed_row_id(org, "booking", c.ref)})).one_or_none()
        label = f"booking {c.ref}"
    else:
        uid = await _who(conn, me, c.latest_for or "me")
        row = (await conn.execute(text(base + "WHERE b.user_id = :u ORDER BY b.booking_number DESC LIMIT 1"), {"u": uid})).one_or_none()
        label = f"latest booking of {c.latest_for}"
    if row is None:
        return [f"{label}: there is none"]
    label += f" ({org.booking_prefix}-{row.booking_number})"
    local = row.starts_at.astimezone(ZoneInfo(org.timezone)).replace(tzinfo=None)
    shown = local.strftime("%Y-%m-%dT%H:%M")
    fails = []
    if c.status and row.status != c.status:
        fails.append(f"{label}: status {row.status}, expected {c.status}")
    if c.start_local and shown != c.start_local:
        fails.append(f"{label}: starts {shown} local, expected {c.start_local}")
    if c.start_local_between:
        lo, hi = (datetime.fromisoformat(x) for x in c.start_local_between)
        if not lo <= local < hi:
            fails.append(f"{label}: starts {shown} local, expected in [{c.start_local_between[0]}, {c.start_local_between[1]})")
    if c.practitioner and row.practitioner != c.practitioner:
        fails.append(f"{label}: practitioner {row.practitioner}, expected {c.practitioner}")
    if c.service and row.service != c.service:
        fails.append(f"{label}: service {row.service}, expected {c.service}")
    if c.cancellation_fee is not None:
        want = None if c.cancellation_fee == "null" else Decimal(c.cancellation_fee)
        if row.cancellation_fee != want:
            fails.append(f"{label}: cancellation fee {row.cancellation_fee}, expected {c.cancellation_fee}")
    return fails
