"""Admin metrics: a fixed set of read-only queries, one function per metric.

There is no text-to-SQL anywhere: the model picks a metric (a tool) and a window, and the
SQL here is fixed. Every function runs on the caller's connection, which the tools open
with readonly_tx (app_readonly, READ ONLY, app.org_id set), so RLS limits it to the
admin's own org; each query also filters on org_id = app_org() explicitly.

Every metric returns the same shape:
  {"metric", "title", "values", "window": {name, label, start, end, timezone}, "definition"}
so the model (and the metric card) can always state the window and definition of a number.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import LLM_PRICES_USD_PER_1M, LLM_PRICING_NOTE
from app.domain.analytics.windows import Window
from app.domain.money import format_money, money_str
from app.tenancy.orgs import Org

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ORDER_STATUSES = ("pending", "confirmed", "shipped", "delivered", "cancelled")
BOOKING_STATUSES = ("confirmed", "completed", "cancelled")
DEFAULT_LIMIT = 5

Values = dict[str, Any]
Compute = Callable[[AsyncConnection, Org, Window, int], Awaitable[Values]]


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    title: str
    definition: str  # one sentence, stated with every number
    verticals: frozenset[str]  # which org verticals have it (keyed on vertical, never slug)
    compute: Compute
    ranked: bool = False  # takes a `limit` (top-N lists)


def _pct(part: int | Decimal, whole: int | Decimal) -> str | None:
    """'66.7' (one decimal, half up), or None when the whole is 0 (no rate, not 0%)."""
    if not whole:
        return None
    return str((Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _money(value: Decimal | None, org: Org) -> dict[str, str]:
    v = Decimal(value or 0)
    return {"amount": money_str(v), "display": format_money(v, org.currency)}


def _w(w: Window) -> dict[str, Any]:
    return {"start": w.start, "end": w.end}


# ───────────── store ─────────────


async def _orders_by_status(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    rows = dict((await conn.execute(text(
        "SELECT status, count(*) FROM orders WHERE org_id = app_org() "
        "AND created_at >= :start AND created_at < :end GROUP BY status"), _w(w))).all())  # fmt: skip
    by_status = {s: rows.get(s, 0) for s in ORDER_STATUSES}
    return {"total": sum(by_status.values()), "by_status": by_status}


async def _revenue(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    total, n = (await conn.execute(text(
        "SELECT coalesce(sum(total), 0), count(*) FROM orders WHERE org_id = app_org() "
        "AND status <> 'cancelled' AND created_at >= :start AND created_at < :end"), _w(w))).one()  # fmt: skip
    return {"revenue": _money(total, org), "orders": n}


async def _average_order_value(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    v = await _revenue(conn, org, w, limit)
    n = v["orders"]
    aov = _money(Decimal(v["revenue"]["amount"]) / n, org) if n else None
    return {"average_order_value": aov, "orders": n, "revenue": v["revenue"]}


async def _top_products(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    rows = (await conn.execute(text(
        "SELECT i.sku, i.name, sum(i.qty) AS units, sum(i.qty * i.unit_price) AS revenue "
        "FROM order_items i JOIN orders o ON o.id = i.order_id "
        "WHERE o.org_id = app_org() AND o.status <> 'cancelled' "
        "AND o.created_at >= :start AND o.created_at < :end "
        "GROUP BY i.sku, i.name ORDER BY units DESC, revenue DESC, i.sku LIMIT :n"),
        {**_w(w), "n": limit})).all()  # fmt: skip
    return {"products": [{"sku": r.sku, "name": r.name, "units": int(r.units), "revenue": _money(r.revenue, org)}
                         for r in rows]}  # fmt: skip


async def _most_out_of_stock(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    rows = (await conn.execute(text(
        "SELECT e.sku, p.name, p.stock, count(*) AS events, sum(e.requested_qty - e.available_qty) AS short "
        "FROM stock_events e LEFT JOIN products p ON p.org_id = e.org_id AND p.sku = e.sku "
        "WHERE e.org_id = app_org() AND e.created_at >= :start AND e.created_at < :end "
        "GROUP BY e.sku, p.name, p.stock ORDER BY events DESC, short DESC, e.sku LIMIT :n"),
        {**_w(w), "n": limit})).all()  # fmt: skip
    return {"products": [{"sku": r.sku, "name": r.name or r.sku, "shortage_events": r.events,
                          "units_short": int(r.short), "stock_now": r.stock} for r in rows]}  # fmt: skip


# ───────────── booking ─────────────


async def _bookings_by_status(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    rows = dict((await conn.execute(text(
        "SELECT status, count(*) FROM bookings WHERE org_id = app_org() "
        "AND starts_at >= :start AND starts_at < :end GROUP BY status"), _w(w))).all())  # fmt: skip
    by_status = {s: rows.get(s, 0) for s in BOOKING_STATUSES}
    return {"total": sum(by_status.values()), "by_status": by_status}


async def _cancellations(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    n, with_fee, fees = (await conn.execute(text(
        "SELECT count(*), count(*) FILTER (WHERE coalesce(cancellation_fee, 0) > 0), "
        "coalesce(sum(cancellation_fee), 0) FROM bookings WHERE org_id = app_org() "
        "AND status = 'cancelled' AND starts_at >= :start AND starts_at < :end"), _w(w))).one()  # fmt: skip
    return {"cancellations": n, "with_fee": with_fee, "fees": _money(fees, org)}


async def _open_minutes(conn: AsyncConnection, w: Window) -> int:
    """Minutes the studio is open in the window: weekly hours, holidays excluded."""
    hours: dict[int, tuple[time, time]] = {
        r.weekday: (r.opens, r.closes)
        for r in await conn.execute(text("SELECT weekday, opens, closes FROM opening_hours WHERE org_id = app_org()"))
    }
    holidays = set((await conn.execute(text(
        "SELECT day FROM holidays WHERE org_id = app_org() AND day BETWEEN :a AND :b"),
        {"a": w.first_day, "b": w.last_day})).scalars())  # fmt: skip
    total = 0
    for day in w.days():
        if day in holidays or day.weekday() not in hours:
            continue
        opens, closes = hours[day.weekday()]
        total += (closes.hour * 60 + closes.minute) - (opens.hour * 60 + opens.minute)
    return total


async def _practitioner_utilisation(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    open_min = await _open_minutes(conn, w)
    # Booked time is clipped to the window, so a booking across midnight counts only its part.
    # FILTER, not just the LEFT JOIN: least()/greatest() skip NULLs, so a practitioner with no
    # booking would otherwise count the whole window as booked.
    rows = (await conn.execute(text(
        "SELECT p.name, coalesce(sum(EXTRACT(EPOCH FROM least(b.ends_at, :end) - greatest(b.starts_at, :start)) / 60) "
        "FILTER (WHERE b.id IS NOT NULL), 0) AS booked "
        "FROM practitioners p LEFT JOIN bookings b ON b.practitioner_id = p.id AND b.org_id = p.org_id "
        "AND b.status IN ('confirmed', 'completed') AND b.starts_at < :end AND b.ends_at > :start "
        "WHERE p.org_id = app_org() GROUP BY p.id, p.name ORDER BY booked DESC, p.name"), _w(w))).all()  # fmt: skip
    values: Values = {
        "open_minutes": open_min,
        "practitioners": [{"practitioner": r.name, "booked_minutes": int(r.booked), "open_minutes": open_min,
                           "utilisation_pct": _pct(int(r.booked), open_min)} for r in rows],
    }  # fmt: skip
    if open_min == 0:
        # Said in words, so a model doesn't turn the null rate into "0%" or a ranking.
        values["note"] = ("The studio was closed for the whole window (no open minutes: closed day or holiday), "
                          "so utilisation is not defined (not 0%). Say so, and offer a longer window such as this_week.")  # fmt: skip
    return values


async def _top_services(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    rows = (await conn.execute(text(
        "SELECT s.name, count(*) AS n, sum(b.price) AS revenue FROM bookings b JOIN services s ON s.id = b.service_id "
        "WHERE b.org_id = app_org() AND b.status <> 'cancelled' AND b.starts_at >= :start AND b.starts_at < :end "
        "GROUP BY s.name ORDER BY n DESC, revenue DESC, s.name LIMIT :n"), {**_w(w), "n": limit})).all()  # fmt: skip
    return {"services": [{"service": r.name, "bookings": r.n, "revenue": _money(r.revenue, org)} for r in rows]}


async def _busiest_slots(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    rows = (await conn.execute(text(
        "SELECT EXTRACT(ISODOW FROM starts_at AT TIME ZONE :tz)::int AS dow, "
        "EXTRACT(HOUR FROM starts_at AT TIME ZONE :tz)::int AS hour, count(*) AS n FROM bookings "
        "WHERE org_id = app_org() AND status <> 'cancelled' AND starts_at >= :start AND starts_at < :end "
        "GROUP BY dow, hour ORDER BY n DESC, dow, hour LIMIT :n"), {**_w(w), "tz": org.timezone, "n": limit})).all()  # fmt: skip
    return {"slots": [{"weekday": WEEKDAY_NAMES[r.dow - 1], "hour": f"{r.hour:02d}:00", "bookings": r.n} for r in rows]}


# ───────────── both verticals ─────────────


async def _active_customers(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    n = await conn.scalar(text(
        "SELECT count(DISTINCT m.user_id) FROM messages m JOIN users u ON u.id = m.user_id "
        "WHERE m.org_id = app_org() AND m.role = 'user' AND u.role = 'customer' "
        "AND m.created_at >= :start AND m.created_at < :end"), _w(w))  # fmt: skip
    return {"active_customers": n}


async def _new_signups(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    n = await conn.scalar(text(
        "SELECT count(*) FROM users WHERE org_id = app_org() AND role = 'customer' "
        "AND created_at >= :start AND created_at < :end"), _w(w))  # fmt: skip
    return {"new_signups": n}


# What counts as a conversion, per vertical: the table whose rows carry conversation_id.
_CONVERTED_IN = {"store": "orders", "booking": "bookings"}


async def _conversion_rate(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    table = _CONVERTED_IN[org.vertical]  # from this module's own constant, never input
    total, converted = (await conn.execute(text(
        f"SELECT count(*), count(*) FILTER (WHERE EXISTS (SELECT 1 FROM {table} x "  # noqa: S608
        "  WHERE x.org_id = c.org_id AND x.conversation_id = c.id)) "
        "FROM conversations c WHERE c.org_id = app_org() AND c.kind = 'customer' "
        "AND c.created_at >= :start AND c.created_at < :end"), _w(w))).one()  # fmt: skip
    return {"conversations": total, "converted": converted, "conversion_rate_pct": _pct(converted, total)}


def _cost(model: str, tokens_in: int, tokens_out: int) -> Decimal | None:
    price = LLM_PRICES_USD_PER_1M.get(model)
    if price is None:
        return None
    return (Decimal(tokens_in) * Decimal(price[0]) + Decimal(tokens_out) * Decimal(price[1])) / Decimal(1_000_000)


def _usd(v: Decimal) -> str:
    return str(v.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


async def _ai_usage(conn: AsyncConnection, org: Org, w: Window, limit: int) -> Values:
    rows = (await conn.execute(text(
        "SELECT model, count(*) AS runs, sum(input_tokens) AS tin, sum(output_tokens) AS tout FROM agent_runs "
        "WHERE org_id = app_org() AND created_at >= :start AND created_at < :end GROUP BY model ORDER BY runs DESC, model"),
        _w(w))).all()  # fmt: skip
    models, total, unpriced = [], Decimal(0), []
    for r in rows:
        cost = _cost(r.model, int(r.tin), int(r.tout))
        if cost is None:
            unpriced.append(r.model)
        else:
            total += cost
        models.append({"model": r.model, "runs": r.runs, "input_tokens": int(r.tin), "output_tokens": int(r.tout),
                       "estimated_cost_usd": _usd(cost) if cost is not None else None})  # fmt: skip
    return {
        "runs": sum(m["runs"] for m in models),
        "input_tokens": sum(m["input_tokens"] for m in models),
        "output_tokens": sum(m["output_tokens"] for m in models),
        "estimated_cost_usd": _usd(total),
        "pricing_note": LLM_PRICING_NOTE,
        "unpriced_models": unpriced,
        "by_model": models,
    }


STORE, BOOKING, BOTH = frozenset({"store"}), frozenset({"booking"}), frozenset({"store", "booking"})

METRICS: dict[str, Metric] = {m.name: m for m in (
    Metric("orders_by_status", "Orders by status", "Orders placed in the window, counted by their current status.", STORE, _orders_by_status),
    Metric("revenue", "Revenue", "Sum of order totals for orders placed in the window, excluding cancelled orders (cash on delivery, so not necessarily collected yet).", STORE, _revenue),
    Metric("average_order_value", "Average order value", "Revenue (non-cancelled orders placed in the window) divided by the number of those orders.", STORE, _average_order_value),
    Metric("top_products", "Top products", "Products ranked by units ordered in non-cancelled orders placed in the window.", STORE, _top_products, ranked=True),
    Metric("most_out_of_stock", "Most out-of-stock", "Products ranked by how often a customer asked for more units than were in stock during the window (one shortage event per request).", STORE, _most_out_of_stock, ranked=True),
    Metric("bookings_by_status", "Bookings by status", "Appointments scheduled to start in the window, counted by their current status.", BOOKING, _bookings_by_status),
    Metric("cancellations", "Cancellations", "Cancelled appointments that were scheduled to start in the window, with the total cancellation fees recorded on them.", BOOKING, _cancellations),
    Metric("practitioner_utilisation", "Practitioner utilisation", "Booked minutes (confirmed or completed appointments, clipped to the window) divided by the studio's open minutes in the window (opening hours minus holidays), per practitioner.", BOOKING, _practitioner_utilisation),
    Metric("top_services", "Top services", "Services ranked by non-cancelled appointments scheduled to start in the window.", BOOKING, _top_services, ranked=True),
    Metric("busiest_slots", "Busiest time slots", "Weekday and start-hour buckets (org time) ranked by non-cancelled appointments starting in the window.", BOOKING, _busiest_slots, ranked=True),
    Metric("active_customers", "Active customers", "Distinct customers who sent at least one chat message in the window.", BOTH, _active_customers),
    Metric("new_signups", "New sign-ups", "Customer accounts created in the window.", BOTH, _new_signups),
    Metric("conversion_rate", "Conversion rate", "Customer conversations started in the window that have at least one order or booking created in that conversation, divided by all customer conversations started in the window.", BOTH, _conversion_rate),
    Metric("ai_usage", "AI usage", f"Assistant turns (customer and admin chats) run in the window, with tokens and cost {LLM_PRICING_NOTE}.", BOTH, _ai_usage),
)}  # fmt: skip


async def compute(conn: AsyncConnection, org: Org, name: str, window: Window, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """One metric for the org, in the standard shape. KeyError for a metric the org's
    vertical doesn't have (the registry never offers one)."""
    metric = METRICS[name]
    if org.vertical not in metric.verticals:
        raise KeyError(name)
    values = await metric.compute(conn, org, window, limit)
    return {"metric": name, "title": metric.title, "values": values, "window": window.as_dict(),
            "definition": metric.definition}  # fmt: skip


# ───────────── "How did we do?" ─────────────

# The sections of activity_summary per vertical (keyed on vertical, never on a slug). Each
# is an existing metric, computed by compute() above: the summary adds no numbers of its own.
SUMMARY_SECTIONS: dict[str, list[str]] = {
    "store": ["orders_by_status", "revenue", "average_order_value", "active_customers", "new_signups", "conversion_rate"],
    "booking": ["bookings_by_status", "cancellations", "active_customers", "new_signups", "conversion_rate"],
}

SUMMARY_DEFINITION = (
    "The headline metrics for the window, one section each; every section carries its own "
    "definition, exactly as when that metric is asked for on its own."
)


async def activity_summary(conn: AsyncConnection, org: Org, window: Window) -> dict[str, Any]:
    """Every headline metric of the org's vertical for one window, on one (read-only)
    connection. A section is byte-for-byte what compute() returns for that metric."""
    sections = [await compute(conn, org, name, window) for name in SUMMARY_SECTIONS[org.vertical]]
    return {"metric": "activity_summary", "title": "Activity summary", "window": window.as_dict(),
            "definition": SUMMARY_DEFINITION, "sections": sections}  # fmt: skip
