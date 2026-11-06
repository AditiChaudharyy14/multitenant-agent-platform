"""`make advance ORDER=HT-1042`: move an order one status on (demo / admin).

The prefix picks the org (orgs.order_prefix is unique), so no slug is needed. Runs as
app_user in an admin-scoped transaction for that org: no owner credentials, and RLS
still applies. Every step writes order_status_history with changed_by 'admin'.
"""

import argparse
import asyncio
import re
import sys

from sqlalchemy import text

from app.db.session import TenantContext, app_engine, tenant_tx
from app.domain.orders.admin import CannotAdvance, OrderNotFound, advance
from app.domain.orders.numbers import display_number, parse_order_number


async def run(raw: str) -> int:
    m = re.match(r"^\s*([A-Za-z]{2,4})", raw)
    if not m:
        print("usage: make advance ORDER=<PREFIX-NUMBER>, e.g. HT-1042", file=sys.stderr)
        return 1
    prefix = m.group(1).upper()
    async with app_engine.connect() as conn:  # orgs is global: app_user may read it
        org_id = await conn.scalar(text("SELECT id FROM orgs WHERE order_prefix = :p"), {"p": prefix})
    number = parse_order_number(raw, prefix)
    if org_id is None or number is None:
        print(f"no order {raw}", file=sys.stderr)
        return 1
    try:
        async with tenant_tx(TenantContext(org_id=org_id, user_id=None, role="admin")) as conn:
            before, after = await advance(conn, number)
    except OrderNotFound:
        print(f"no order {raw}", file=sys.stderr)
        return 1
    except CannotAdvance as exc:
        print(f"{display_number(prefix, number)} is {exc.status}: it can't advance", file=sys.stderr)
        return 2
    finally:
        await app_engine.dispose()
    print(f"{display_number(prefix, number)}: {before} -> {after}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("order", help="order number, e.g. HT-1042")
    return asyncio.run(run(p.parse_args().order))


if __name__ == "__main__":
    sys.exit(main())
