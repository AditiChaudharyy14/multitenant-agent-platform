"""`make seed`: idempotent, consistent with the domain rules, and anchored on SEED_NOW."""

import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings, require_owner_url
from app.domain.kb.chunker import load_kb_dir
from app.seed.run import WIPE_ORDER, resolve_seed_now, seed_all
from tests.conftest import SEED_NOW
from tests.factories import TENANT_TABLES, set_owner_tenant

SEED = Path(get_settings().seed_dir)
TZ = ZoneInfo("Asia/Kathmandu")
ANCHOR = datetime(2026, 11, 9, 10, 0, tzinfo=TZ)


async def snapshot(owner: AsyncEngine, org_id: uuid.UUID) -> dict:
    """Everything that must be identical after a reseed with the same SEED_NOW."""
    async with owner.begin() as conn:
        await set_owner_tenant(conn, org_id)
        counts = {
            t: await conn.scalar(text(f"SELECT count(*) FROM {t} WHERE org_id = :o"), {"o": org_id})
            for t in TENANT_TABLES
        }
        # With the id: search breaks score ties on it, so a reseed must keep it.
        chunks = sorted((await conn.execute(text("SELECT id, doc_id, section FROM kb_chunks"))).all())
        orders = sorted((await conn.execute(text("SELECT id, status, total, created_at FROM orders"))).all())
        history = sorted(
            (await conn.execute(text("SELECT order_id, to_status, changed_at FROM order_status_history"))).all()
        )
        bookings = sorted(
            (await conn.execute(text("SELECT id, booking_number, status, starts_at, ends_at FROM bookings"))).all()
        )
        products = sorted((await conn.execute(text("SELECT sku, price, stock FROM products"))).all())
    return {
        "counts": counts, "chunks": chunks, "orders": orders,
        "history": history, "bookings": bookings, "products": products,
    }  # fmt: skip


def test_wipe_covers_every_tenant_table() -> None:
    """A new tenant table that the seeder forgets to wipe would make reseeding non-idempotent."""
    assert sorted(WIPE_ORDER) == sorted(TENANT_TABLES)


def test_seed_now_parsing() -> None:
    assert resolve_seed_now("2026-11-09T10:00", "Asia/Kathmandu") == ANCHOR
    # An explicit offset is respected.
    assert resolve_seed_now("2026-11-09T04:15:00+00:00", "Asia/Kathmandu") == ANCHOR
    # Unset or empty falls back to the clock.
    assert resolve_seed_now(None, "Asia/Kathmandu") is None
    assert resolve_seed_now("", "Asia/Kathmandu") is None
    with pytest.raises(ValueError):
        resolve_seed_now("next tuesday", "Asia/Kathmandu")


async def test_reseeding_is_idempotent(seeded: dict[str, uuid.UUID], owner_engine: AsyncEngine) -> None:
    # Reseed first: earlier tests may have added rows (e.g. sign-ups) to the seeded orgs.
    await seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW, analytics=False)
    before = {v: await snapshot(owner_engine, o) for v, o in seeded.items()}
    await seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW, analytics=False)
    after = {v: await snapshot(owner_engine, o) for v, o in seeded.items()}
    assert after == before
    for v in seeded:
        assert before[v]["counts"]["kb_chunks"] > 0
        assert before[v]["counts"]["users"] == 4


async def test_reseeding_kills_existing_sessions(seeded: dict[str, uuid.UUID], owner_engine: AsyncEngine) -> None:
    org = seeded["store"]
    async with owner_engine.begin() as conn:
        await set_owner_tenant(conn, org)
        uid = await conn.scalar(text("SELECT id FROM users WHERE role = 'customer' LIMIT 1"))
        await conn.execute(
            text(
                "INSERT INTO sessions (token_hash, user_id, org_id, role, expires_at) "
                "VALUES (:h, :u, :o, 'customer', now() + interval '1 day')"
            ),
            {"h": uuid.uuid4().bytes, "u": uid, "o": org},
        )
    await seed_all(require_owner_url(), get_settings().seed_dir, SEED_NOW, analytics=False)
    async with owner_engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM sessions WHERE org_id = :o"), {"o": org}) == 0


@pytest.mark.parametrize("vertical", ["store", "booking"])
async def test_catalog_names_match_data(seeded: dict[str, uuid.UUID], owner_engine: AsyncEngine, vertical: str) -> None:
    """KB catalog headings == relational names, found through front matter, never by slug."""
    org = seeded[vertical]
    async with owner_engine.connect() as conn:
        slug = await conn.scalar(text("SELECT slug FROM orgs WHERE id = :o"), {"o": org})
    catalogs = [d for d in load_kb_dir(SEED / slug / "kb") if d.catalog_of]
    assert catalogs, "each org needs a catalog document"
    async with owner_engine.begin() as conn:
        await set_owner_tenant(conn, org)
        for d in catalogs:
            names = set(await conn.scalars(text(f"SELECT name FROM {d.catalog_of}")))  # noqa: S608
            assert {c.heading for c in d.chunks} == names
            if d.catalog_of == "products":
                for sku, name in (await conn.execute(text("SELECT sku, name FROM products"))).all():
                    chunk = next(c for c in d.chunks if c.heading == name)
                    assert f"SKU: {sku}" in chunk.content


async def test_store_orders_are_consistent(seeded: dict[str, uuid.UUID], owner_engine: AsyncEngine) -> None:
    async with owner_engine.begin() as conn:
        await set_owner_tenant(conn, seeded["store"])
        orders = (
            await conn.execute(
                text(
                    "SELECT o.id, o.status, o.total, o.created_at, o.updated_at, u.email "
                    "FROM orders o JOIN users u ON u.id = o.user_id"
                )
            )
        ).all()
        by_email: dict[str, list[str]] = {}
        for oid, status, total, created, updated, email in orders:
            by_email.setdefault(email, []).append(status)
            hist = (
                await conn.execute(
                    text(
                        "SELECT from_status, to_status, changed_at FROM order_status_history "
                        "WHERE order_id = :o ORDER BY changed_at"
                    ),
                    {"o": oid},
                )
            ).all()
            assert hist[0][:2] == (None, "pending") and hist[0][2] == created
            assert hist[-1][1] == status and hist[-1][2] == updated
            for prev, cur in zip(hist, hist[1:], strict=False):
                assert cur[0] == prev[1] and cur[2] > prev[2]
            assert updated <= ANCHOR
            items_total = await conn.scalar(
                text("SELECT sum(unit_price * qty) FROM order_items WHERE order_id = :o"), {"o": oid}
            )
            assert items_total == total
        stock = dict((await conn.execute(text("SELECT name, stock FROM products"))).all())
    assert sorted(map(sorted, by_email.values())) == [["delivered", "pending"], ["delivered", "shipped"]]
    assert len(by_email) == 2  # the third customer has no orders
    assert sum(1 for s in stock.values() if s == 0) == 1
    assert sum(1 for s in stock.values() if 0 < s <= 2) == 2
    assert len(stock) == 22


async def test_bookings_respect_schedule(seeded: dict[str, uuid.UUID], owner_engine: AsyncEngine) -> None:
    async with owner_engine.begin() as conn:
        await set_owner_tenant(conn, seeded["booking"])
        hours = {
            wd: (o, c)
            for wd, o, c in (await conn.execute(text("SELECT weekday, opens, closes FROM opening_hours"))).all()
        }
        holidays = set(await conn.scalars(text("SELECT day FROM holidays")))
        rows = (
            await conn.execute(
                text(
                    "SELECT b.status, b.starts_at, b.ends_at, b.price, s.duration_min, s.price, u.email, "
                    "EXISTS (SELECT 1 FROM practitioner_services ps "
                    "        WHERE ps.practitioner_id = b.practitioner_id AND ps.service_id = b.service_id) "
                    "FROM bookings b JOIN services s ON s.id = b.service_id JOIN users u ON u.id = b.user_id"
                )
            )
        ).all()
    assert 5 not in hours, "closed on Saturday"
    assert hours[4][1] < hours[0][1], "Friday closes early"
    assert len(holidays) == 3
    per_customer: dict[str, list[str]] = {}
    for status, start, end, price, duration, service_price, email, offered in rows:
        local_start, local_end = start.astimezone(TZ), end.astimezone(TZ)
        assert offered and price == service_price
        assert end - start == timedelta(minutes=duration)
        assert local_start.minute % 15 == 0
        assert local_start.date() not in holidays
        opens, closes = hours[local_start.weekday()]
        assert opens <= local_start.time() and local_end.time() <= closes
        if status == "confirmed":
            assert start > ANCHOR
        else:
            assert status == "completed" and end < ANCHOR
        per_customer.setdefault(email, []).append(status)
    assert sorted(map(sorted, per_customer.values())) == [["completed", "confirmed"]] * 2
    # 2026-11-11 is a holiday: the booking two days after the anchor rolled forward past it.
    assert datetime(2026, 11, 11).date() in holidays


def test_seed_data_has_no_slug_specific_keys() -> None:
    """data.yaml shape depends on vertical only (invariant 11)."""
    for path in SEED.glob("*/data.yaml"):
        assert set(yaml.safe_load(path.read_text())) <= {
            "users", "products", "orders", "services", "practitioners",
            "opening_hours", "holidays", "bookings", "canonical_facts",
        }  # fmt: skip


async def test_seed_bookings_get_numbers_from_1001(seeded: dict[str, uuid.UUID], owner_engine: AsyncEngine) -> None:
    """Seed bookings are numbered by the same per-org counter trigger as new ones, in data.yaml
    order, and the wipe restarts the counter, so the numbers are identical on every reseed."""
    async with owner_engine.begin() as conn:
        await set_owner_tenant(conn, seeded["booking"])
        numbers = list(await conn.scalars(text("SELECT booking_number FROM bookings ORDER BY booking_number")))
        last = await conn.scalar(text("SELECT last_number FROM booking_counters"))
        prefix = await conn.scalar(text("SELECT booking_prefix FROM orgs WHERE id = :o"), {"o": seeded["booking"]})
    assert numbers == list(range(1001, 1001 + len(numbers))) and numbers
    assert last == numbers[-1]
    assert prefix == "LW"
