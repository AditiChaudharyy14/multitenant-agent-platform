"""Test data builders and helpers shared by the isolation and concurrency tests.

Fixture rows are written as app_owner. FORCE ROW LEVEL SECURITY applies to the owner
too, so every owner transaction first sets app.org_id (and role admin) exactly like a
request would.
"""

import itertools
import string
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.session import TenantContext

# Every table that carries org_id and must be under RLS (sessions is the documented
# exception: it is looked up by token hash before the org is known).
TENANT_TABLES = (
    "users", "products", "orders", "order_items", "order_status_history",
    "services", "practitioners", "practitioner_services", "opening_hours", "holidays",
    "bookings", "conversations", "messages", "proposals", "agent_runs", "kb_chunks",
    "order_counters", "booking_counters", "stock_events",
)  # fmt: skip

# Customer-owned tables: also carry user_id and a RESTRICTIVE own-rows policy.
CUSTOMER_TABLES = (
    "orders", "order_items", "order_status_history", "bookings",
    "conversations", "messages", "proposals", "stock_events",
)  # fmt: skip

# Far enough ahead that no fixture booking ever lands near "now".
BASE_TIME = datetime(2030, 1, 7, 4, 15, tzinfo=UTC)  # 10:00 in Asia/Kathmandu

ZERO_VECTOR = "[" + ",".join(["0"] * 384) + "]"


@dataclass
class Tenant:
    org_id: uuid.UUID
    admin: uuid.UUID
    x: uuid.UUID  # customer X
    y: uuid.UUID  # customer Y
    product: uuid.UUID
    service: uuid.UUID
    spare_service: uuid.UUID  # linked to no practitioner, so a new link to it is valid
    p1: uuid.UUID
    p2: uuid.UUID
    orders: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)  # user -> order
    conversations: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)  # user -> conv
    bookings: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)  # user -> booking

    def ctx(self, user: uuid.UUID | None, role: str) -> TenantContext:
        return TenantContext(org_id=self.org_id, user_id=user, role=role)

    def admin_ctx(self) -> TenantContext:
        return self.ctx(self.admin, "admin")


def new_id() -> uuid.UUID:
    return uuid.uuid4()


_prefix_counter = itertools.count()


def unique_prefix() -> str:
    """A fresh 4-letter order prefix (orgs.order_prefix is unique). The test schema is
    rebuilt every run, so a per-process counter is enough: TAAA, TAAB, ..."""
    n, letters = next(_prefix_counter), []
    for _ in range(3):
        n, r = divmod(n, 26)
        letters.append(string.ascii_uppercase[r])
    return "T" + "".join(reversed(letters))


async def set_owner_tenant(conn: AsyncConnection, org_id: uuid.UUID) -> None:
    """The owner is subject to FORCE RLS, so it acts as this org's admin."""
    await conn.execute(
        text(
            "SELECT set_config('app.org_id', :o, true), set_config('app.user_id', '', true), "
            "set_config('app.role', 'admin', true)"
        ),
        {"o": str(org_id)},
    )


async def insert_user(conn: AsyncConnection, org_id: uuid.UUID, role: str) -> uuid.UUID:
    uid = new_id()
    await conn.execute(
        text(
            "INSERT INTO users (id, org_id, email, display_name, password_hash, role) "
            "VALUES (:id, :o, :email, :name, 'x', :role)"
        ),
        {"id": uid, "o": org_id, "email": f"{uid.hex}@example.test", "name": role, "role": role},
    )
    return uid


async def insert_booking(
    conn: AsyncConnection,
    t: "Tenant",
    user: uuid.UUID,
    practitioner: uuid.UUID,
    starts_at: datetime,
    minutes: int = 60,
    status: str = "confirmed",
) -> uuid.UUID:
    bid = new_id()
    await conn.execute(
        text(
            "INSERT INTO bookings (id, org_id, user_id, service_id, practitioner_id, "
            "starts_at, ends_at, status, price) "
            "VALUES (:id, :o, :u, :s, :p, :start, :end, :status, 1500)"
        ),
        {
            "id": bid, "o": t.org_id, "u": user, "s": t.service, "p": practitioner,
            "start": starts_at, "end": starts_at + timedelta(minutes=minutes), "status": status,
        },
    )  # fmt: skip
    return bid


async def seed_tenant(owner: AsyncEngine, vertical: str) -> Tenant:
    """One org with at least one row in every tenant table, for two customers X and Y."""
    org_id = new_id()
    async with owner.begin() as conn:
        # orgs has no RLS (it's global), so it needs no settings.
        await conn.execute(
            text(
                "INSERT INTO orgs (id, slug, name, vertical, order_prefix, booking_prefix) "
                "VALUES (:id, :slug, :name, :v, :p, :bp)"
            ),
            {"id": org_id, "slug": f"test-{org_id.hex}", "name": "Test org", "v": vertical,
             "p": unique_prefix() if vertical == "store" else None,
             "bp": unique_prefix() if vertical == "booking" else None},  # fmt: skip
        )
        await set_owner_tenant(conn, org_id)

        admin = await insert_user(conn, org_id, "admin")
        x = await insert_user(conn, org_id, "customer")
        y = await insert_user(conn, org_id, "customer")

        product, service, spare, p1, p2 = new_id(), new_id(), new_id(), new_id(), new_id()
        await conn.execute(
            text(
                "INSERT INTO products (id, org_id, sku, name, category, price, stock) "
                "VALUES (:id, :o, 'SKU-1', 'Tea', 'tea', 450, 10)"
            ),
            {"id": product, "o": org_id},
        )
        await conn.execute(
            text(
                "INSERT INTO services (id, org_id, name, duration_min, price) VALUES "
                "(:s, :o, 'Massage', 60, 1500), (:spare, :o, 'Facial', 30, 900)"
            ),
            {"s": service, "spare": spare, "o": org_id},
        )
        await conn.execute(
            text("INSERT INTO practitioners (id, org_id, name) VALUES (:p1, :o, 'P1'), (:p2, :o, 'P2')"),
            {"p1": p1, "p2": p2, "o": org_id},
        )
        await conn.execute(
            text(
                "INSERT INTO practitioner_services (org_id, practitioner_id, service_id) "
                "VALUES (:o, :p1, :s), (:o, :p2, :s)"
            ),
            {"o": org_id, "p1": p1, "p2": p2, "s": service},
        )
        await conn.execute(
            text(
                "INSERT INTO opening_hours (org_id, weekday, opens, closes) "
                "VALUES (:o, 0, '09:00', '18:00')"
            ),
            {"o": org_id},
        )
        await conn.execute(
            text("INSERT INTO holidays (org_id, day, name) VALUES (:o, '2030-01-01', 'New Year')"),
            {"o": org_id},
        )
        await conn.execute(
            text(
                "INSERT INTO kb_chunks (org_id, doc_id, doc_title, section, content, embedding) "
                "VALUES (:o, 'faq', 'FAQ', 'hours', 'We open at nine.', CAST(:v AS vector))"
            ),
            {"o": org_id, "v": ZERO_VECTOR},
        )

        t = Tenant(org_id, admin, x, y, product, service, spare, p1, p2)
        # X and Y each get a full set of customer-owned rows; on different practitioners
        # so their bookings don't trip the overlap constraints.
        for user, practitioner in ((x, p1), (y, p2)):
            order, conv = new_id(), new_id()
            await conn.execute(
                text(
                    "INSERT INTO orders (id, org_id, user_id, delivery_address, total) "
                    "VALUES (:id, :o, :u, 'Thamel', 450)"
                ),
                {"id": order, "o": org_id, "u": user},
            )
            await conn.execute(
                text(
                    "INSERT INTO order_items (org_id, user_id, order_id, product_id, sku, name, "
                    "unit_price, qty) VALUES (:o, :u, :ord, :p, 'SKU-1', 'Tea', 450, 1)"
                ),
                {"o": org_id, "u": user, "ord": order, "p": product},
            )
            await conn.execute(
                text(
                    "INSERT INTO order_status_history (org_id, user_id, order_id, to_status, "
                    "changed_by) VALUES (:o, :u, :ord, 'pending', 'customer')"
                ),
                {"o": org_id, "u": user, "ord": order},
            )
            await conn.execute(
                text("INSERT INTO conversations (id, org_id, user_id) VALUES (:id, :o, :u)"),
                {"id": conv, "o": org_id, "u": user},
            )
            await conn.execute(
                text(
                    "INSERT INTO messages (org_id, user_id, conversation_id, role, content) "
                    "VALUES (:o, :u, :c, 'user', 'hello')"
                ),
                {"o": org_id, "u": user, "c": conv},
            )
            await conn.execute(
                text(
                    "INSERT INTO proposals (org_id, user_id, conversation_id, kind, payload, "
                    "summary, created_turn_id, expires_at) VALUES (:o, :u, :c, 'create_order', "
                    "'{}', '{}', :turn, now() + interval '1 hour')"
                ),
                {"o": org_id, "u": user, "c": conv, "turn": new_id()},
            )
            await conn.execute(
                text(
                    "INSERT INTO agent_runs (org_id, user_id, conversation_id, provider, model, "
                    "status) VALUES (:o, :u, :c, 'mistral', 'test', 'ok')"
                ),
                {"o": org_id, "u": user, "c": conv},
            )
            await conn.execute(
                text(
                    "INSERT INTO stock_events (org_id, user_id, sku, requested_qty, available_qty, "
                    "conversation_id) VALUES (:o, :u, 'SKU-1', 20, 10, :c)"
                ),
                {"o": org_id, "u": user, "c": conv},
            )
            t.orders[user] = order
            t.conversations[user] = conv
            t.bookings[user] = await insert_booking(conn, t, user, practitioner, BASE_TIME)
    return t


async def add_customers(owner: AsyncEngine, t: Tenant, n: int) -> list[uuid.UUID]:
    async with owner.begin() as conn:
        await set_owner_tenant(conn, t.org_id)
        return [await insert_user(conn, t.org_id, "customer") for _ in range(n)]


def valid_row_sql(table: str, t: Tenant) -> tuple[str, dict]:
    """An INSERT that would be fully valid for org `t` (FKs, uniques, checks all satisfied).

    Used to prove that RLS alone is what rejects a cross-tenant write.
    """
    o, x = t.org_id, t.x
    rows: dict[str, tuple[str, dict]] = {
        "users": (
            "INSERT INTO users (org_id, email, display_name, password_hash, role) "
            "VALUES (:o, :e, 'n', 'x', 'customer')",
            {"e": f"{new_id().hex}@example.test"},
        ),
        "products": (
            "INSERT INTO products (org_id, sku, name, category, price, stock) "
            "VALUES (:o, :sku, 'n', 'c', 1, 1)",
            {"sku": new_id().hex},
        ),
        "orders": (
            "INSERT INTO orders (org_id, user_id, delivery_address, total) VALUES (:o, :x, 'a', 1)",
            {},
        ),
        "order_items": (
            "INSERT INTO order_items (org_id, user_id, order_id, product_id, sku, name, "
            "unit_price, qty) VALUES (:o, :x, :ord, :p, 's', 'n', 1, 1)",
            {"ord": t.orders[x], "p": t.product},
        ),
        "order_status_history": (
            "INSERT INTO order_status_history (org_id, user_id, order_id, to_status, changed_by) "
            "VALUES (:o, :x, :ord, 'pending', 'system')",
            {"ord": t.orders[x]},
        ),
        "services": (
            "INSERT INTO services (org_id, name, duration_min, price) VALUES (:o, :n, 15, 1)",
            {"n": new_id().hex},
        ),
        "practitioners": ("INSERT INTO practitioners (org_id, name) VALUES (:o, 'n')", {}),
        "practitioner_services": (
            "INSERT INTO practitioner_services (org_id, practitioner_id, service_id) "
            "VALUES (:o, :p, :s)",
            {"p": t.p1, "s": t.spare_service},
        ),
        "opening_hours": (
            "INSERT INTO opening_hours (org_id, weekday, opens, closes) VALUES (:o, 3, '09:00', '17:00')",
            {},
        ),
        "holidays": ("INSERT INTO holidays (org_id, day, name) VALUES (:o, '2030-12-25', 'h')", {}),
        "bookings": (
            "INSERT INTO bookings (org_id, user_id, service_id, practitioner_id, starts_at, "
            "ends_at, price) VALUES (:o, :x, :s, :p, CAST(:start AS timestamptz), "
            "CAST(:start AS timestamptz) + interval '1 hour', 1)",
            {"s": t.service, "p": t.p1, "start": BASE_TIME + timedelta(days=30)},
        ),
        "conversations": ("INSERT INTO conversations (org_id, user_id) VALUES (:o, :x)", {}),
        "messages": (
            "INSERT INTO messages (org_id, user_id, conversation_id, role) VALUES (:o, :x, :c, 'user')",
            {"c": t.conversations[x]},
        ),
        "proposals": (
            # 'expired', not 'open': each conversation may hold only one open proposal.
            "INSERT INTO proposals (org_id, user_id, conversation_id, kind, payload, summary, "
            "status, created_turn_id, expires_at) VALUES (:o, :x, :c, 'k', '{}', '{}', 'expired', :turn, now())",
            {"c": t.conversations[x], "turn": new_id()},
        ),
        "agent_runs": (
            "INSERT INTO agent_runs (org_id, user_id, conversation_id, provider, model, status) "
            "VALUES (:o, :x, :c, 'p', 'm', 'ok')",
            {"c": t.conversations[x]},
        ),
        "kb_chunks": (
            "INSERT INTO kb_chunks (org_id, doc_id, doc_title, section, content, embedding) "
            "VALUES (:o, 'd', 't', :sec, 'c', CAST(:v AS vector))",
            {"sec": new_id().hex, "v": ZERO_VECTOR},
        ),
        "stock_events": (
            "INSERT INTO stock_events (org_id, user_id, sku, requested_qty, available_qty) "
            "VALUES (:o, :x, 'SKU-1', 5, 1)",
            {},
        ),
        # The org already has a counter (its fixture orders created it), hence the upsert.
        "order_counters": (
            "INSERT INTO order_counters AS c (org_id, last_number) VALUES (:o, 5000) "
            "ON CONFLICT (org_id) DO UPDATE SET last_number = c.last_number + 1",
            {},
        ),
        # Likewise: the fixture bookings already created this org's booking counter.
        "booking_counters": (
            "INSERT INTO booking_counters AS c (org_id, last_number) VALUES (:o, 5000) "
            "ON CONFLICT (org_id) DO UPDATE SET last_number = c.last_number + 1",
            {},
        ),
    }
    sql, params = rows[table]
    return sql, {"o": o, "x": x, **params}


def pg_cause(excinfo: pytest.ExceptionInfo[DBAPIError]) -> asyncpg.PostgresError:
    """The asyncpg exception underneath SQLAlchemy's wrapper, for precise assertions."""
    cause = excinfo.value.orig.__cause__
    assert isinstance(cause, asyncpg.PostgresError), repr(excinfo.value)
    return cause


# ───────────── Store shop: products with known prices and stock ─────────────

# (sku, name, category, price, stock). BLK-1 has one unit left, BLK-2 is sold out.
SHOP_PRODUCTS = (
    ("GRN-1", "Green Alpha", "green", "800.00", 10),
    ("GRN-2", "Green Beta", "green", "900.00", 5),
    ("GRN-3", "Green Gamma", "green", "1500.00", 3),
    ("BLK-1", "Black Alpha", "black", "600.00", 1),
    ("BLK-2", "Black Beta", "black", "700.00", 0),
    ("BLK-3", "Black Gamma", "black", "650.00", 20),
)  # fmt: skip


@dataclass
class Shop:
    org_id: uuid.UUID
    prefix: str
    admin: uuid.UUID
    x: uuid.UUID
    y: uuid.UUID
    products: dict[str, uuid.UUID]  # sku -> id
    conversations: dict[uuid.UUID, uuid.UUID]  # user -> conversation

    def number(self, n: int) -> str:
        return f"{self.prefix}-{n}"


async def seed_shop(owner: AsyncEngine) -> Shop:
    """A store org with SHOP_PRODUCTS, an admin and customers X and Y (one chat each), no orders."""
    org_id = new_id()
    prefix = unique_prefix()
    async with owner.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO orgs (id, slug, name, vertical, timezone, order_prefix, currency) "
                "VALUES (:id, :slug, 'Test shop', 'store', 'Asia/Kathmandu', :p, 'NPR')"
            ),
            {"id": org_id, "slug": f"shop-{org_id.hex}", "p": prefix},
        )
        await set_owner_tenant(conn, org_id)
        admin = await insert_user(conn, org_id, "admin")
        x = await insert_user(conn, org_id, "customer")
        y = await insert_user(conn, org_id, "customer")
        products = {}
        for sku, name, category, price, stock in SHOP_PRODUCTS:
            products[sku] = await conn.scalar(
                text(
                    "INSERT INTO products (org_id, sku, name, category, price, stock) "
                    "VALUES (:o, :sku, :n, :c, CAST(:p AS numeric), :s) RETURNING id"
                ),
                {"o": org_id, "sku": sku, "n": name, "c": category, "p": price, "s": stock},
            )
        conversations = {}
        for user in (x, y):
            conversations[user] = await conn.scalar(
                text("INSERT INTO conversations (org_id, user_id) VALUES (:o, :u) RETURNING id"),
                {"o": org_id, "u": user},
            )
    return Shop(org_id, prefix, admin, x, y, products, conversations)


async def insert_shop_order(
    owner: AsyncEngine,
    shop: Shop,
    user: uuid.UUID,
    items: list[tuple[str, int]],
    *,
    status: str = "pending",
    address: str = "House 1, Thamel, Kathmandu",
    created_at: datetime | None = None,
) -> int:
    """An existing order written directly (as if placed earlier), at today's catalogue
    prices, with a history row per step up to `status`. Stock is left untouched: the
    fixture's stock already accounts for it. Returns the trigger-assigned order number."""
    flow = ["pending", "confirmed", "shipped", "delivered"]
    steps = ["pending", "cancelled"] if status == "cancelled" else flow[: flow.index(status) + 1]
    at = created_at or datetime(2026, 11, 1, 4, 15, tzinfo=UTC)
    async with owner.begin() as conn:
        await set_owner_tenant(conn, shop.org_id)
        prices = dict(
            (await conn.execute(text("SELECT sku, price FROM products WHERE org_id = :o"), {"o": shop.org_id})).all()
        )
        names = dict(
            (await conn.execute(text("SELECT sku, name FROM products WHERE org_id = :o"), {"o": shop.org_id})).all()
        )
        oid, number = (
            await conn.execute(
                text(
                    "INSERT INTO orders (org_id, user_id, status, delivery_address, total, created_at, updated_at) "
                    "VALUES (:o, :u, :s, :a, :t, :at, :at) RETURNING id, order_number"
                ),
                {"o": shop.org_id, "u": user, "s": status, "a": address,
                 "t": sum(prices[sku] * q for sku, q in items), "at": at},  # fmt: skip
            )
        ).one()
        for sku, qty in items:
            await conn.execute(
                text(
                    "INSERT INTO order_items (org_id, user_id, order_id, product_id, sku, name, unit_price, qty) "
                    "VALUES (:o, :u, :ord, :p, :sku, :n, :price, :q)"
                ),
                {"o": shop.org_id, "u": user, "ord": oid, "p": shop.products[sku], "sku": sku,
                 "n": names[sku], "price": prices[sku], "q": qty},  # fmt: skip
            )
        previous = None
        for n, step in enumerate(steps):
            await conn.execute(
                text(
                    "INSERT INTO order_status_history (org_id, user_id, order_id, from_status, to_status, "
                    "changed_at, changed_by) VALUES (:o, :u, :ord, :f, :t, :at, 'system')"
                ),
                {"o": shop.org_id, "u": user, "ord": oid, "f": previous, "t": step,
                 "at": at + timedelta(hours=n)},  # fmt: skip
            )
            previous = step
    return number


# ───────────── Booking studio: known hours, holiday, services and practitioners ─────────────

# Local (Asia/Kathmandu) weekly hours: Mon–Thu 09–19, Fri 09–15, Saturday closed, Sun 09–19.
STUDIO_HOURS = {0: ("09:00", "19:00"), 1: ("09:00", "19:00"), 2: ("09:00", "19:00"),
                3: ("09:00", "19:00"), 4: ("09:00", "15:00"), 6: ("09:00", "19:00")}  # fmt: skip
STUDIO_HOLIDAY = "2026-11-11"  # a Wednesday
# key -> (name, minutes, price)
STUDIO_SERVICES = {
    "swedish": ("Swedish Massage", 60, "3500.00"),
    "deep": ("Deep Tissue Massage", 90, "5200.00"),
    "yoga": ("Private Yoga", 45, "2200.00"),
}  # fmt: skip
# key -> (name, service keys). Asha sorts first, so she is the auto-pick when both are free.
STUDIO_PRACTITIONERS = {
    "asha": ("Asha Gurung", ("swedish", "deep")),
    "bina": ("Bina Shrestha", ("swedish", "deep", "yoga")),
}  # fmt: skip


@dataclass
class Studio:
    org_id: uuid.UUID
    prefix: str
    admin: uuid.UUID
    x: uuid.UUID
    y: uuid.UUID
    services: dict[str, uuid.UUID]  # key -> id
    practitioners: dict[str, uuid.UUID]  # key -> id
    conversations: dict[uuid.UUID, uuid.UUID]  # user -> conversation

    def number(self, n: int) -> str:
        return f"{self.prefix}-{n}"


async def seed_studio(owner: AsyncEngine) -> Studio:
    """A booking org with STUDIO_* hours, holiday, services and practitioners; customers X
    and Y with one chat each; no bookings."""
    org_id = new_id()
    prefix = unique_prefix()
    async with owner.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO orgs (id, slug, name, vertical, timezone, booking_prefix, currency) "
                "VALUES (:id, :slug, 'Test studio', 'booking', 'Asia/Kathmandu', :p, 'NPR')"
            ),
            {"id": org_id, "slug": f"studio-{org_id.hex}", "p": prefix},
        )
        await set_owner_tenant(conn, org_id)
        admin = await insert_user(conn, org_id, "admin")
        x = await insert_user(conn, org_id, "customer")
        y = await insert_user(conn, org_id, "customer")
        services = {}
        for key, (name, minutes, price) in STUDIO_SERVICES.items():
            services[key] = await conn.scalar(
                text(
                    "INSERT INTO services (org_id, name, duration_min, price) "
                    "VALUES (:o, :n, :m, CAST(:p AS numeric)) RETURNING id"
                ),
                {"o": org_id, "n": name, "m": minutes, "p": price},
            )
        practitioners = {}
        for key, (name, offers) in STUDIO_PRACTITIONERS.items():
            practitioners[key] = await conn.scalar(
                text("INSERT INTO practitioners (org_id, name) VALUES (:o, :n) RETURNING id"), {"o": org_id, "n": name}
            )
            for s in offers:
                await conn.execute(
                    text("INSERT INTO practitioner_services (org_id, practitioner_id, service_id) VALUES (:o, :p, :s)"),
                    {"o": org_id, "p": practitioners[key], "s": services[s]},
                )
        for weekday, (opens, closes) in STUDIO_HOURS.items():
            await conn.execute(
                text(
                    "INSERT INTO opening_hours (org_id, weekday, opens, closes) "
                    "VALUES (:o, :w, :a, :b)"
                ),
                {"o": org_id, "w": weekday, "a": time.fromisoformat(opens), "b": time.fromisoformat(closes)},
            )
        await conn.execute(
            text("INSERT INTO holidays (org_id, day, name) VALUES (:o, :d, 'Tihar')"),
            {"o": org_id, "d": date.fromisoformat(STUDIO_HOLIDAY)},
        )
        conversations = {}
        for user in (x, y):
            conversations[user] = await conn.scalar(
                text("INSERT INTO conversations (org_id, user_id) VALUES (:o, :u) RETURNING id"),
                {"o": org_id, "u": user},
            )
    return Studio(org_id, prefix, admin, x, y, services, practitioners, conversations)


async def add_studio_customers(owner: AsyncEngine, studio: Studio, n: int) -> list[uuid.UUID]:
    """n more customers, each with a conversation (recorded in studio.conversations)."""
    async with owner.begin() as conn:
        await set_owner_tenant(conn, studio.org_id)
        users = []
        for _ in range(n):
            user = await insert_user(conn, studio.org_id, "customer")
            studio.conversations[user] = await conn.scalar(
                text("INSERT INTO conversations (org_id, user_id) VALUES (:o, :u) RETURNING id"),
                {"o": studio.org_id, "u": user},
            )
            users.append(user)
    return users


async def insert_studio_booking(
    owner: AsyncEngine, studio: Studio, user: uuid.UUID, service: str, practitioner: str,
    starts_at: datetime, *, status: str = "confirmed",
) -> int:  # fmt: skip
    """An existing booking written directly; returns its trigger-assigned booking number."""
    _, minutes, price = STUDIO_SERVICES[service]
    async with owner.begin() as conn:
        await set_owner_tenant(conn, studio.org_id)
        return await conn.scalar(
            text(
                "INSERT INTO bookings (org_id, user_id, service_id, practitioner_id, starts_at, ends_at, "
                "status, price, created_at) VALUES (:o, :u, :s, :p, :start, :end, :status, "
                "CAST(:price AS numeric), :created) RETURNING booking_number"
            ),
            {"o": studio.org_id, "u": user, "s": studio.services[service], "p": studio.practitioners[practitioner],
             "start": starts_at, "end": starts_at + timedelta(minutes=minutes), "status": status, "price": price,
             "created": starts_at - timedelta(days=7)},
        )  # fmt: skip
