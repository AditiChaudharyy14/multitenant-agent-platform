"""`make create-admin ORG=<slug> EMAIL=<email>`: create an admin user (sign-up only makes
customers). Prompts for the password.

Connects as app_user, not app_owner: the insert runs in an admin-scoped tenant_tx, so RLS
and the users_admin_guard trigger both still apply.
"""

import argparse
import asyncio
import getpass
import sys

import asyncpg
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth.passwords import hash_password
from app.auth.schemas import MIN_PASSWORD_LENGTH, normalize_email
from app.db import session as db
from app.tenancy.orgs import get_org_by_slug


async def create_admin(slug: str, email: str, password: str, display_name: str) -> None:
    async with db.app_engine.connect() as conn:
        org = await get_org_by_slug(conn, slug)
    if org is None:
        raise SystemExit(f"no org with slug {slug!r} (did you run make sync-orgs?)")
    password_hash = await hash_password(password)
    try:
        async with db.tenant_tx(db.TenantContext(org.id, None, "admin")) as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (org_id, email, display_name, password_hash, role) "
                    "VALUES (:o, :e, :n, :h, 'admin')"
                ),
                {"o": org.id, "e": email, "n": display_name, "h": password_hash},
            )
    except IntegrityError as exc:
        if isinstance(exc.orig.__cause__, asyncpg.UniqueViolationError):
            raise SystemExit(f"{email} is already registered in {slug}") from None
        raise
    finally:
        await db.app_engine.dispose()
    print(f"created admin {email} in {slug}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True, help="org slug")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="Admin", help="display name")
    args = parser.parse_args()
    try:
        email = normalize_email(args.email)
    except ValueError as exc:
        sys.exit(str(exc))
    password = getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if getpass.getpass("Repeat password: ") != password:
        sys.exit("passwords do not match")
    asyncio.run(create_admin(args.org, email, password, args.name))


if __name__ == "__main__":
    main()
