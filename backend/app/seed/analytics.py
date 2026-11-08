"""seed/<slug>/analytics.yaml: three weeks of history so the admin metrics aren't all zero.

Loaded by `make seed` after data.yaml. It is a separate file (and a separate step) so the
eval template can leave it out: history orders and bookings would shift the numbers new
orders and bookings get (HT-1007 → HT-1027), and every recorded customer cassette would
stop matching. Admin eval scenarios add it to their own clone with `analytics_history`.

Everything is relative to the seed "now" in the org's timezone and deterministic (uuid5
ids from the seed keys). Rows dated after "now" are left out, so a morning `make seed`
still works; order history steps after "now" are left out too. The history has its own
customers, so the demo accounts in data.yaml keep clean chat lists.

  customers:      accounts with `joined: {days_ago, at}` (recent ones are "new sign-ups")
  conversations:  a customer's chat: `started`, user `messages` (each gets an assistant
                  reply and an agent_runs row), and optionally the `order` (store) or
                  `booking` (booking) created in it
  stock_events:   shortages: a customer asked for `requested` units, `available` were left
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.seed.data import HistoryStep, OrderItem, SeedData, SeedDataError, SeedUser, _require, _unique

ASSISTANT_REPLY = "Glad to help. Is there anything else you need?"
FILE_NAME = "analytics.yaml"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class When(_Model):
    days_ago: int = Field(ge=0, le=60)
    at: time  # org-local wall-clock time


class AnalyticsCustomer(SeedUser):
    role: Literal["customer"] = "customer"
    joined: When


class HistoryOrder(_Model):
    items: list[OrderItem] = Field(min_length=1)
    delivery_address: str
    history: list[HistoryStep] = []


class HistoryBooking(_Model):
    service: str  # service key
    practitioner: str  # practitioner key
    days_ago: int = Field(ge=0, le=60)  # moved back to the nearest open day if needed
    at: time
    status: Literal["completed", "cancelled"]  # history only: never a future confirmed slot
    fee_pct: Literal[0, 50] = 0  # cancellation fee as a % of the price
    booked_days_before: int = Field(default=2, ge=0, le=14)  # when the chat that made it started


class HistoryConversation(_Model):
    key: str
    customer: str
    started: When | None = None  # derived from the booking when it has one
    messages: list[str] = Field(min_length=1)
    order: HistoryOrder | None = None
    booking: HistoryBooking | None = None

    @model_validator(mode="after")
    def _when(self) -> "HistoryConversation":
        _require(
            self.order is None or self.booking is None, f"conversation {self.key}: an order or a booking, not both"
        )
        _require((self.started is None) == (self.booking is not None),
                 f"conversation {self.key}: give `started`, or a booking (whose chat date is derived)")  # fmt: skip
        return self


class HistoryStockEvent(_Model):
    customer: str
    sku: str
    requested: int = Field(gt=0)
    available: int = Field(ge=0)
    at: When
    conversation: str | None = None

    @model_validator(mode="after")
    def _short(self) -> "HistoryStockEvent":
        _require(self.available < self.requested, f"stock event {self.sku}: available must be below requested")
        return self


class AnalyticsData(_Model):
    model: str = "gemini-3.5-flash-lite"  # agent_runs.model for the seeded turns
    customers: list[AnalyticsCustomer] = []
    conversations: list[HistoryConversation] = []
    stock_events: list[HistoryStockEvent] = []

    def check(self, base: SeedData, vertical: str) -> None:
        """Cross-references against data.yaml; keyed on vertical, never on slug."""
        _unique("analytics customer key", [c.key for c in self.customers] + [u.key for u in base.users])
        _unique("email", [c.email.lower() for c in self.customers] + [u.email.lower() for u in base.users])
        _unique("conversation key", [c.key for c in self.conversations])
        customers = {c.key for c in self.customers} | {u.key for u in base.users if u.role == "customer"}
        skus = {p.sku for p in base.products}
        offers = {p.key: set(p.services) for p in base.practitioners}
        conversations = {c.key for c in self.conversations}
        for c in self.conversations:
            _require(c.customer in customers, f"conversation {c.key}: unknown customer {c.customer!r}")
            if c.order:
                _require(vertical == "store", f"conversation {c.key}: orders only for a store org")
                for i in c.order.items:
                    _require(i.sku in skus, f"conversation {c.key}: unknown sku {i.sku!r}")
            if c.booking:
                _require(vertical == "booking", f"conversation {c.key}: bookings only for a booking org")
                b = c.booking
                _require(
                    b.service in offers.get(b.practitioner, set()),
                    f"conversation {c.key}: practitioner doesn't offer service",
                )
                _require(b.at.minute % 15 == 0, f"conversation {c.key}: booking not on the 15-minute grid")
        for e in self.stock_events:
            _require(vertical == "store", "stock_events only for a store org")
            _require(e.customer in customers, f"stock event: unknown customer {e.customer!r}")
            _require(e.sku in skus, f"stock event: unknown sku {e.sku!r}")
            _require(
                e.conversation is None or e.conversation in conversations,
                f"stock event: unknown conversation {e.conversation!r}",
            )


def load_analytics(org_dir: Path, base: SeedData, vertical: str) -> AnalyticsData | None:
    path = org_dir / FILE_NAME
    if not path.exists():
        return None
    data = AnalyticsData.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    data.check(base, vertical)
    return data


@dataclass
class Rows:
    """Rows per table, in FK order (users first)."""

    tables: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {
        t: [] for t in ("users", "conversations", "messages", "agent_runs", "orders", "order_items",
                        "order_status_history", "bookings", "stock_events")})  # fmt: skip


def _rid(org_id: uuid.UUID, kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(org_id, f"analytics:{kind}:{key}")


def _local(tz: ZoneInfo, day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=tz).astimezone(UTC)


def build_rows(
    org_id: uuid.UUID, data: AnalyticsData, base: SeedData, user_ids: dict[str, uuid.UUID],
    hashes: dict[str, str], now: datetime, tz: ZoneInfo,
) -> Rows:  # fmt: skip
    """Every analytics row for one org. `user_ids` holds data.yaml's users (key → id)."""
    from app.seed.run import CHANGED_BY  # the same changed_by rule as the base seed

    today = now.astimezone(tz).date()
    out = Rows()
    t = out.tables
    users = dict(user_ids)
    for cust in data.customers:
        users[cust.key] = _rid(org_id, "user", cust.key)
        joined = _local(tz, today - timedelta(days=cust.joined.days_ago), cust.joined.at)
        if joined > now:
            continue  # joins later today: not yet a customer
        t["users"].append({"id": users[cust.key], "org_id": org_id, "email": cust.email.lower(), "display_name": cust.display_name,
                           "password_hash": hashes[cust.key], "role": "customer", "created_at": joined})  # fmt: skip
    present = {u["id"] for u in t["users"]} | set(user_ids.values())

    products = {p.sku: p for p in base.products}
    services = {s.key: s for s in base.services}
    hours = {h.weekday: h for h in base.opening_hours}  # absent weekday = closed
    holidays = {h.day for h in base.holidays}
    conv_ids: dict[str, uuid.UUID] = {}

    for c in data.conversations:
        uid = users[c.customer]
        if uid not in present:
            continue
        booking_row = None
        if c.booking:
            b = c.booking
            service = services[b.service]
            day = today - timedelta(days=b.days_ago)
            # Back to the nearest earlier day where the whole appointment fits: open, not a
            # holiday, inside the hours (Friday closes early). History only moves backward.
            for _ in range(14):
                h = hours.get(day.weekday())
                starts = _local(tz, day, b.at)
                ends = starts + timedelta(minutes=service.duration_min)
                local_end = ends.astimezone(tz)
                if (h and day not in holidays and b.at >= h.opens and local_end.date() == day
                        and local_end.time() <= h.closes):  # fmt: skip
                    break
                day -= timedelta(days=1)
            else:
                raise SeedDataError(f"conversation {c.key}: no day within two weeks fits the booking")
            if ends > now:
                continue  # not over yet at the seed "now": history only
            started = _local(tz, day - timedelta(days=b.booked_days_before), time(8, 30))
            booking_row = {
                "id": _rid(org_id, "booking", c.key), "org_id": org_id, "user_id": uid,
                "service_id": uuid.uuid5(org_id, f"service:{b.service}"),
                "practitioner_id": uuid.uuid5(org_id, f"practitioner:{b.practitioner}"),
                "starts_at": starts, "ends_at": ends, "status": b.status, "price": service.price,
                "cancellation_fee": (service.price * b.fee_pct / 100).quantize(Decimal("0.01")) if b.status == "cancelled" else None,
                "created_at": started + timedelta(minutes=2 * len(c.messages)),
            }  # fmt: skip
        else:
            assert c.started is not None
            started = _local(tz, today - timedelta(days=c.started.days_ago), c.started.at)
        if started > now:
            continue
        cid = conv_ids[c.key] = _rid(org_id, "conversation", c.key)
        last = started + timedelta(minutes=2 * len(c.messages))
        t["conversations"].append({"id": cid, "org_id": org_id, "user_id": uid, "kind": "customer",
                                   "title": " ".join(c.messages[0].split()[:6]), "created_at": started,
                                   "updated_at": min(last, now)})  # fmt: skip
        for n, msg in enumerate(c.messages):
            asked = started + timedelta(minutes=2 * n)
            if asked > now:
                break
            replied = asked + timedelta(minutes=1)
            t["messages"].append({"id": _rid(org_id, "message", f"{c.key}:{n}:u"), "org_id": org_id, "user_id": uid,
                                  "conversation_id": cid, "role": "user", "content": msg, "created_at": asked})  # fmt: skip
            t["messages"].append({"id": _rid(org_id, "message", f"{c.key}:{n}:a"), "org_id": org_id, "user_id": uid,
                                  "conversation_id": cid, "role": "assistant", "content": ASSISTANT_REPLY,
                                  "created_at": replied})  # fmt: skip
            t["agent_runs"].append({"id": _rid(org_id, "run", f"{c.key}:{n}"), "org_id": org_id, "user_id": uid,
                                    "conversation_id": cid, "provider": "gemini", "model": data.model,
                                    "input_tokens": 1800 + 350 * n, "output_tokens": 90 + 15 * len(msg) // 10,
                                    "latency_ms": 1400, "status": "ok", "created_at": replied})  # fmt: skip
        if booking_row:
            booking_row["conversation_id"] = cid
            t["bookings"].append(booking_row)
        if c.order:
            placed = last + timedelta(minutes=1)
            if placed > now:
                continue
            oid = _rid(org_id, "order", c.key)
            steps = [("pending", placed)] + [(s.to, placed + timedelta(hours=s.after_hours)) for s in c.order.history]
            steps = [s for s in steps if s[1] <= now]  # history up to the seed "now" only
            items = [(products[i.sku], i.qty) for i in c.order.items]
            t["orders"].append({"id": oid, "org_id": org_id, "user_id": uid, "status": steps[-1][0],
                                "delivery_address": c.order.delivery_address, "payment_method": "cod",
                                "total": sum(p.price * q for p, q in items), "conversation_id": cid,
                                "created_at": placed, "updated_at": steps[-1][1]})  # fmt: skip
            for p, qty in items:
                t["order_items"].append({"id": _rid(org_id, "order_item", f"{c.key}:{p.sku}"), "org_id": org_id,
                                         "user_id": uid, "order_id": oid, "product_id": uuid.uuid5(org_id, f"product:{p.sku}"),
                                         "sku": p.sku, "name": p.name, "unit_price": p.price, "qty": qty})  # fmt: skip
            previous = None
            for n, (status, at) in enumerate(steps):
                t["order_status_history"].append({"id": _rid(org_id, "order_status", f"{c.key}:{n}"), "org_id": org_id,
                                                  "user_id": uid, "order_id": oid, "from_status": previous,
                                                  "to_status": status, "changed_at": at, "changed_by": CHANGED_BY[status]})  # fmt: skip
                previous = status

    for n, e in enumerate(data.stock_events):
        uid = users[e.customer]
        at = _local(tz, today - timedelta(days=e.at.days_ago), e.at.at)
        if at > now or uid not in present:
            continue
        t["stock_events"].append({"id": _rid(org_id, "stock_event", str(n)), "org_id": org_id, "user_id": uid,
                                  "sku": e.sku, "requested_qty": e.requested, "available_qty": e.available,
                                  "conversation_id": conv_ids.get(e.conversation or ""), "created_at": at})  # fmt: skip
    return out
