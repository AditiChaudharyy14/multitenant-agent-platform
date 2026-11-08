"""seed/<slug>/data.yaml schema and validation.

Which sections are required depends on org.vertical, never on the slug. Every cross-
reference (customer keys, SKUs, services a practitioner offers, status transitions) is
checked here, so a bad seed file fails before anything is written.
"""

from datetime import date, time
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth.schemas import MIN_PASSWORD_LENGTH, normalize_email

# Order status flow from CLAUDE.md; every transition becomes an order_status_history row.
ORDER_TRANSITIONS = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"shipped"},
    "shipped": {"delivered"},
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeedUser(_Model):
    key: str
    email: str
    display_name: str
    role: Literal["customer", "admin"]
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)  # DEMO ONLY, see data.yaml
    # users.created_at, counted back from the seed "now": the admin's new_signups metric.
    joined_days_ago: int = Field(default=45, ge=0)

    @model_validator(mode="after")
    def _email(self) -> "SeedUser":
        normalize_email(self.email)  # same shape rule as signup
        return self


class Product(_Model):
    sku: str
    name: str
    category: str
    price: Decimal = Field(ge=0)
    stock: int = Field(ge=0)


class LocalTime(_Model):
    days_ago: int = Field(ge=0)
    at: time  # org-local wall-clock time


class HistoryStep(_Model):
    to: Literal["confirmed", "shipped", "delivered", "cancelled"]
    after_hours: float = Field(gt=0)  # since the order was placed


class OrderItem(_Model):
    sku: str
    qty: int = Field(gt=0)


class SeedOrder(_Model):
    key: str
    customer: str
    delivery_address: str
    placed: LocalTime
    items: list[OrderItem] = Field(min_length=1)
    history: list[HistoryStep] = []

    @property
    def status(self) -> str:
        return self.history[-1].to if self.history else "pending"


class Service(_Model):
    key: str
    name: str
    duration_min: int = Field(gt=0, multiple_of=15)
    price: Decimal = Field(ge=0)


class Practitioner(_Model):
    key: str
    name: str
    services: list[str] = Field(min_length=1)


class OpeningHours(_Model):
    weekday: int = Field(ge=0, le=6)  # 0 = Monday
    opens: time
    closes: time


class Holiday(_Model):
    day: date  # absolute, org-local
    name: str


class SeedBooking(_Model):
    key: str
    customer: str
    service: str
    practitioner: str
    day_offset: int  # days from the seed "now"; negative = past
    at: time
    status: Literal["confirmed", "completed", "cancelled"]


class CanonicalFact(_Model):
    """One fact the KB must state consistently (app/domain/kb/consistency.py)."""

    mentions: str  # regex, lower case: what a sentence is about, e.g. "unopened"
    value: str  # the one true value, e.g. "fourteen days", "50%"


class SeedData(_Model):
    users: list[SeedUser] = Field(min_length=1)
    products: list[Product] = []
    orders: list[SeedOrder] = []
    services: list[Service] = []
    practitioners: list[Practitioner] = []
    opening_hours: list[OpeningHours] = []
    holidays: list[Holiday] = []
    bookings: list[SeedBooking] = []
    canonical_facts: list[CanonicalFact] = []

    @model_validator(mode="after")
    def _cross_references(self) -> "SeedData":
        _unique("user key", [u.key for u in self.users])
        _unique("email", [u.email.lower() for u in self.users])
        _unique("sku", [p.sku for p in self.products])
        _unique("product name", [p.name for p in self.products])
        _unique("order key", [o.key for o in self.orders])
        _unique("service key", [s.key for s in self.services])
        _unique("service name", [s.name for s in self.services])
        _unique("practitioner key", [p.key for p in self.practitioners])
        _unique("weekday", [h.weekday for h in self.opening_hours])
        _unique("holiday", [h.day for h in self.holidays])
        _unique("booking key", [b.key for b in self.bookings])

        customers = {u.key for u in self.users if u.role == "customer"}
        skus = {p.sku for p in self.products}
        for o in self.orders:
            _require(o.customer in customers, f"order {o.key}: unknown customer {o.customer!r}")
            for item in o.items:
                _require(item.sku in skus, f"order {o.key}: unknown sku {item.sku!r}")
            _unique(f"order {o.key} sku", [i.sku for i in o.items])
            status, last = "pending", 0.0
            for step in o.history:
                _require(
                    step.to in ORDER_TRANSITIONS.get(status, set()),
                    f"order {o.key}: illegal transition {status} → {step.to}",
                )
                _require(step.after_hours > last, f"order {o.key}: history must move forward in time")
                status, last = step.to, step.after_hours

        services = {s.key for s in self.services}
        offers = {p.key: set(p.services) for p in self.practitioners}
        for p in self.practitioners:
            _require(set(p.services) <= services, f"practitioner {p.key}: unknown service")
        for h in self.opening_hours:
            _require(h.closes > h.opens, f"weekday {h.weekday}: closes must be after opens")
        for b in self.bookings:
            _require(b.customer in customers, f"booking {b.key}: unknown customer {b.customer!r}")
            _require(b.practitioner in offers, f"booking {b.key}: unknown practitioner")
            _require(
                b.service in offers.get(b.practitioner, set()), f"booking {b.key}: practitioner doesn't offer service"
            )
            _require(b.day_offset != 0, f"booking {b.key}: day_offset 0 is ambiguous (past or upcoming?)")
            if b.day_offset > 0:
                _require(b.status == "confirmed", f"booking {b.key}: an upcoming booking must be confirmed")
            else:
                _require(b.status != "confirmed", f"booking {b.key}: a past booking can't still be confirmed")
            _require(b.at.minute % 15 == 0 and b.at.second == 0, f"booking {b.key}: not on the 15-minute grid")
        return self

    def check_vertical(self, vertical: str) -> None:
        """Store orgs carry a catalogue and orders; booking orgs carry a schedule."""
        store_parts = {"products": self.products, "orders": self.orders}
        booking_parts = {
            "services": self.services, "practitioners": self.practitioners,
            "opening_hours": self.opening_hours, "holidays": self.holidays, "bookings": self.bookings,
        }  # fmt: skip
        required: tuple[str, ...]
        if vertical == "store":
            required, forbidden = ("products",), booking_parts
        elif vertical == "booking":
            required, forbidden = ("services", "practitioners", "opening_hours"), store_parts
        else:
            raise SeedDataError(f"no seed rules for vertical {vertical!r}")
        for name in required:
            _require(bool(getattr(self, name)), f"vertical {vertical} needs {name!r}")
        for name, rows in forbidden.items():
            _require(not rows, f"vertical {vertical} must not have {name!r}")


class SeedDataError(ValueError):
    pass


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise SeedDataError(message)


def _unique(what: str, values: list) -> None:
    dupes = sorted({str(v) for v in values if values.count(v) > 1})
    _require(not dupes, f"duplicate {what}: {dupes}")


def load_seed_data(path: Path, vertical: str) -> SeedData:
    data = SeedData.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    data.check_vertical(vertical)
    return data
