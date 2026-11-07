"""The seeder behind `make seed`. Runs as app_owner in the one-shot ops container.

Order of work, so that a bad seed never leaves a half-written database:
  1. For every org, with no DB writes: validate data.yaml, lint and chunk the KB, check
     the catalogs, embed every chunk, hash the demo passwords.
  2. Upsert the orgs table (sync_orgs).
  3. One transaction for all orgs: per org, set app.org_id (FORCE RLS applies to the
     owner too), delete the org's tenant rows, then insert everything again.

Seed times are relative ("3 days ago at 11:15", local time) and resolve against
clock.now(), which SEED_NOW can freeze, so evals see identical dates on every run.
Row ids are uuid5(org_id, kind:key), so a reseed keeps the same ids.
"""

import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app import clock
from app.auth.passwords import hash_password
from app.domain.kb.ingest import PreparedKB, insert_chunks, prepare_kb
from app.seed.analytics import AnalyticsData, build_rows, load_analytics
from app.seed.data import SeedData, SeedDataError, load_seed_data
from app.tenancy.orgs import OrgConfig, load_org_configs, sync_orgs

# Child tables first, so no delete trips a foreign key. Deleting users also cascades the
# org's sessions, so nobody stays logged in as a user who no longer exists.
# tests/domain/test_seed.py checks this list covers every tenant table. Wiping
# order_counters and booking_counters restarts the org's numbers at 1001, so reseeding is
# deterministic.
WIPE_ORDER = (
    "kb_chunks", "customer_memory", "stock_events", "agent_runs", "proposals", "messages", "conversations",
    "order_status_history", "order_items", "orders", "order_counters", "bookings", "booking_counters",
    "practitioner_services", "practitioners", "services", "opening_hours", "holidays",
    "products", "users",
)  # fmt: skip

# Who performed each order transition, for order_status_history.changed_by.
CHANGED_BY = {"pending": "customer", "confirmed": "admin", "shipped": "admin", "delivered": "system", "cancelled": "customer"}  # fmt: skip


@dataclass(frozen=True, slots=True)
class SeededOrg:
    slug: str
    vertical: str
    org_id: uuid.UUID
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _Prepared:
    cfg: OrgConfig
    data: SeedData
    kb: PreparedKB
    password_hashes: dict[str, str]  # user key → argon2id hash (data.yaml and analytics.yaml)
    analytics: AnalyticsData | None  # analytics.yaml, if the org has one


# Argon2 is deliberately slow and every reseed hashes each demo password again; one hash
# per password per process is enough (the salt is random either way).
_HASHES: dict[str, str] = {}


async def _hash(password: str) -> str:
    if password not in _HASHES:
        _HASHES[password] = await hash_password(password)
    return _HASHES[password]


def resolve_seed_now(value: str | None, timezone: str) -> datetime | None:
    """SEED_NOW as an aware datetime: no offset means the org's timezone. Empty → None."""
    if not value or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"SEED_NOW must be an ISO datetime, got {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(timezone))


def _rid(org_id: uuid.UUID, kind: str, key: str) -> uuid.UUID:
    """Stable row id: the same org and seed key give the same uuid on every reseed."""
    return uuid.uuid5(org_id, f"{kind}:{key}")


def _local(tz: ZoneInfo, day: date, at: time) -> datetime:
    """Org-local date + wall-clock time → UTC (storage is always UTC timestamptz)."""
    return datetime.combine(day, at, tzinfo=tz).astimezone(UTC)


def _store_rows(org_id: uuid.UUID, d: SeedData, users: dict[str, uuid.UUID], now: datetime, tz: ZoneInfo) -> dict[str, list[dict]]:
    today = now.astimezone(tz).date()
    products = {p.sku: p for p in d.products}
    rows: dict[str, list[dict]] = {"products": [], "orders": [], "order_items": [], "order_status_history": []}
    product_ids = {p.sku: _rid(org_id, "product", p.sku) for p in d.products}
    for p in d.products:
        rows["products"].append(
            {"id": product_ids[p.sku], "org_id": org_id, "sku": p.sku, "name": p.name,
             "category": p.category, "price": p.price, "stock": p.stock}
        )  # fmt: skip
    for o in d.orders:
        oid, uid = _rid(org_id, "order", o.key), users[o.customer]
        placed = _local(tz, today - timedelta(days=o.placed.days_ago), o.placed.at)
        steps = [("pending", placed)] + [(s.to, placed + timedelta(hours=s.after_hours)) for s in o.history]
        if steps[-1][1] > now:
            raise SeedDataError(f"order {o.key}: its history ends after the seed 'now' ({now.isoformat()})")
        # Item prices are snapshots of today's catalogue price; the total is their sum.
        items = [(products[i.sku], i.qty) for i in o.items]
        rows["orders"].append(
            {"id": oid, "org_id": org_id, "user_id": uid, "status": o.status,
             "delivery_address": o.delivery_address, "payment_method": "cod",
             "total": sum(p.price * q for p, q in items),
             "created_at": placed, "updated_at": steps[-1][1]}
        )  # fmt: skip
        for p, qty in items:
            rows["order_items"].append(
                {"id": _rid(org_id, "order_item", f"{o.key}:{p.sku}"), "org_id": org_id, "user_id": uid,
                 "order_id": oid, "product_id": product_ids[p.sku], "sku": p.sku, "name": p.name,
                 "unit_price": p.price, "qty": qty}
            )  # fmt: skip
        previous = None
        for n, (status, at) in enumerate(steps):
            rows["order_status_history"].append(
                {"id": _rid(org_id, "order_status", f"{o.key}:{n}"), "org_id": org_id, "user_id": uid,
                 "order_id": oid, "from_status": previous, "to_status": status,
                 "changed_at": at, "changed_by": CHANGED_BY[status]}
            )  # fmt: skip
            previous = status
    return rows


def _booking_rows(org_id: uuid.UUID, d: SeedData, users: dict[str, uuid.UUID], now: datetime, tz: ZoneInfo) -> dict[str, list[dict]]:
    today = now.astimezone(tz).date()
    hours = {h.weekday: h for h in d.opening_hours}
    holidays = {h.day for h in d.holidays}
    services = {s.key: s for s in d.services}
    service_ids = {s.key: _rid(org_id, "service", s.key) for s in d.services}
    practitioner_ids = {p.key: _rid(org_id, "practitioner", p.key) for p in d.practitioners}
    rows: dict[str, list[dict]] = {
        "services": [
            {"id": service_ids[s.key], "org_id": org_id, "name": s.name,
             "duration_min": s.duration_min, "price": s.price}
            for s in d.services
        ],
        "practitioners": [{"id": practitioner_ids[p.key], "org_id": org_id, "name": p.name} for p in d.practitioners],
        "practitioner_services": [
            {"org_id": org_id, "practitioner_id": practitioner_ids[p.key], "service_id": service_ids[s]}
            for p in d.practitioners for s in p.services
        ],
        "opening_hours": [
            {"org_id": org_id, "weekday": h.weekday, "opens": h.opens, "closes": h.closes} for h in d.opening_hours
        ],
        "holidays": [{"org_id": org_id, "day": h.day, "name": h.name} for h in d.holidays],
        "bookings": [],
    }  # fmt: skip
    for b in d.bookings:
        service = services[b.service]
        # Move away from today (forward for upcoming, backward for past) to an open day.
        step = 1 if b.day_offset > 0 else -1
        day = today + timedelta(days=b.day_offset)
        for _ in range(14):
            if day.weekday() in hours and day not in holidays:
                break
            day += timedelta(days=step)
        else:
            raise SeedDataError(f"booking {b.key}: no open day within two weeks")
        starts = _local(tz, day, b.at)
        ends = starts + timedelta(minutes=service.duration_min)
        h = hours[day.weekday()]
        local_end = ends.astimezone(tz)
        # The same rule the slot engine enforces: inside opening hours, full duration included.
        if b.at < h.opens or local_end.date() != day or local_end.time() > h.closes:
            raise SeedDataError(f"booking {b.key}: {day} {b.at} + {service.duration_min} min is outside opening hours")
        rows["bookings"].append(
            {"id": _rid(org_id, "booking", b.key), "org_id": org_id, "user_id": users[b.customer],
             "service_id": service_ids[b.service], "practitioner_id": practitioner_ids[b.practitioner],
             "starts_at": starts, "ends_at": ends, "status": b.status, "price": service.price,
             "created_at": min(starts, now) - timedelta(days=3)}
        )  # fmt: skip
    return rows


async def _insert(conn: AsyncConnection, table: str, rows: list[dict[str, Any]]) -> None:
    """Multi-row INSERT. Table and column names come from this module only, never input."""
    if not rows:
        return
    cols = list(rows[0])
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"  # noqa: S608
    await conn.execute(text(sql), rows)


async def _prepare(cfg: OrgConfig, seed_dir: Path) -> _Prepared:
    org_dir = seed_dir / cfg.slug
    data = load_seed_data(org_dir / "data.yaml", cfg.vertical)
    catalogs: dict[str, dict[str, str | None]] = {
        "products": {p.name: p.sku for p in data.products},
        "services": {s.name: None for s in data.services},
    }
    kb = await prepare_kb(org_dir / "kb", catalogs)
    analytics = load_analytics(org_dir, data, cfg.vertical)
    people = [*data.users, *(analytics.customers if analytics else [])]
    hashes = {u.key: await _hash(u.password) for u in people}
    return _Prepared(cfg, data, kb, hashes, analytics)


async def _seed_org(
    conn: AsyncConnection, org_id: uuid.UUID, p: _Prepared, now: datetime, *, analytics: bool = True
) -> dict[str, int]:
    tz = ZoneInfo(p.cfg.timezone)
    # The owner is subject to FORCE RLS like everyone else: act as this org's admin.
    await conn.execute(
        text(
            "SELECT set_config('app.org_id', :o, true), set_config('app.user_id', '', true), "
            "set_config('app.role', 'admin', true)"
        ),
        {"o": str(org_id)},
    )
    for table in WIPE_ORDER:
        # RLS already limits this to the org; the WHERE says so explicitly as well.
        await conn.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org_id})  # noqa: S608

    users = {u.key: _rid(org_id, "user", u.key) for u in p.data.users}
    await _insert(conn, "users", [
        {"id": users[u.key], "org_id": org_id, "email": u.email.lower(), "display_name": u.display_name,
         "password_hash": p.password_hashes[u.key], "role": u.role,
         "created_at": now - timedelta(days=u.joined_days_ago)}
        for u in p.data.users
    ])  # fmt: skip
    builder = _store_rows if p.cfg.vertical == "store" else _booking_rows
    for table, rows in builder(org_id, p.data, users, now, tz).items():  # dict order = FK order
        await _insert(conn, table, rows)
    await insert_chunks(conn, org_id, p.kb)
    # After the base rows, so their order and booking numbers never change.
    if analytics and p.analytics:
        await seed_analytics(conn, org_id, p, users, now)

    return {
        t: await conn.scalar(text(f"SELECT count(*) FROM {t} WHERE org_id = :o"), {"o": org_id})  # noqa: S608
        for t in reversed(WIPE_ORDER)
    }


async def seed_analytics(conn: AsyncConnection, org_id: uuid.UUID, p: _Prepared, users: dict[str, uuid.UUID], now: datetime) -> None:
    """analytics.yaml's history rows for one org, on a connection already scoped to it."""
    assert p.analytics is not None
    rows = build_rows(org_id, p.analytics, p.data, users, p.password_hashes, now, ZoneInfo(p.cfg.timezone))
    for table, table_rows in rows.tables.items():  # FK order
        await _insert(conn, table, table_rows)


async def apply_analytics(conn: AsyncConnection, seed_dir: str | Path, slug: str, org_id: uuid.UUID) -> int:
    """Add analytics.yaml's history to an org that was seeded without it (the eval clones
    of admin scenarios), at clock.now(). Returns the number of rows added."""
    cfg = next(c for c in load_org_configs(seed_dir) if c.slug == slug)
    org_dir = Path(seed_dir) / slug
    data = load_seed_data(org_dir / "data.yaml", cfg.vertical)
    analytics = load_analytics(org_dir, data, cfg.vertical)
    if analytics is None:
        return 0
    hashes = {c.key: await _hash(c.password) for c in analytics.customers}
    users = {u.key: _rid(org_id, "user", u.key) for u in data.users}
    rows = build_rows(org_id, analytics, data, users, hashes, clock.now(), ZoneInfo(cfg.timezone))
    for table, table_rows in rows.tables.items():
        await _insert(conn, table, table_rows)
    return sum(len(r) for r in rows.tables.values())


async def seed_all(
    owner_url: str, seed_dir: str | Path, seed_now: str | None = None, *, analytics: bool = True
) -> list[SeededOrg]:
    """Rebuild every configured org. Idempotent: the same input gives the same rows.
    analytics=False leaves out analytics.yaml (the eval template does)."""
    seed_dir = Path(seed_dir)
    configs = load_org_configs(seed_dir)
    prepared = [await _prepare(cfg, seed_dir) for cfg in configs]  # 1. all checks, no writes

    await sync_orgs(owner_url, configs)  # 2. orgs table
    engine = create_async_engine(owner_url, poolclass=pool.NullPool)
    results = []
    try:
        async with engine.begin() as conn:  # 3. one transaction: all orgs or none
            ids = dict((await conn.execute(text("SELECT slug, id FROM orgs"))).all())
            for p in prepared:
                anchor = resolve_seed_now(seed_now, p.cfg.timezone)
                # freeze() makes every clock.now() in this org's seeding return the anchor.
                with clock.freeze(anchor) if anchor else nullcontext():
                    counts = await _seed_org(conn, ids[p.cfg.slug], p, clock.now(), analytics=analytics)
                results.append(SeededOrg(p.cfg.slug, p.cfg.vertical, ids[p.cfg.slug], counts))
    finally:
        await engine.dispose()
    return results

