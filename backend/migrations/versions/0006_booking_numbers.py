"""Booking numbers, org booking prefix, and the one-booking-per-proposal guard.

The same pattern as 0005's order numbers:
- orgs.booking_prefix (e.g. 'LW') comes from org.yaml and is unique, so a booking
  number like LW-1001 names exactly one org.
- booking_counters: one row per org holding the last booking number handed out. A
  BEFORE INSERT trigger on bookings bumps it with INSERT ... ON CONFLICT DO UPDATE,
  which row-locks the counter, so two concurrent bookings in one org never share a
  number. SECURITY INVOKER: RLS on booking_counters applies to the inserting role.
- bookings.booking_number: per-org, from 1001, UNIQUE (org_id, booking_number).
  Existing bookings are numbered by created_at.
- bookings.proposal_id UNIQUE: one proposal can create at most one booking, even if the
  application's double-confirm check were wrong.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

FIRST_BOOKING_NUMBER = 1001

UPGRADE = [
    "ALTER TABLE orgs ADD COLUMN booking_prefix text UNIQUE CHECK (booking_prefix ~ '^[A-Z]{2,4}$')",
    """
    CREATE TABLE booking_counters (
      org_id       uuid PRIMARY KEY REFERENCES orgs(id),
      last_number  integer NOT NULL CHECK (last_number > 0)
    )
    """,
    "ALTER TABLE booking_counters ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE booking_counters FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON booking_counters USING (org_id = app_org()) WITH CHECK (org_id = app_org())",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON booking_counters TO app_user",
    "GRANT SELECT ON booking_counters TO app_readonly",
    "ALTER TABLE bookings ADD COLUMN booking_number integer",
    "ALTER TABLE bookings ADD COLUMN proposal_id uuid UNIQUE",
    # Backfill. FORCE RLS applies to app_owner too, so act as each org's admin in turn.
    f"""
    DO $$
    DECLARE o record;
    BEGIN
      FOR o IN SELECT id FROM orgs LOOP
        PERFORM set_config('app.org_id', o.id::text, true), set_config('app.role', 'admin', true);
        UPDATE bookings SET booking_number = n.num
          FROM (SELECT id, {FIRST_BOOKING_NUMBER - 1} + row_number() OVER (ORDER BY created_at, id) AS num
                FROM bookings WHERE org_id = o.id) n
          WHERE bookings.id = n.id;
        INSERT INTO booking_counters (org_id, last_number)
          SELECT o.id, max(booking_number) FROM bookings WHERE org_id = o.id HAVING count(*) > 0;
      END LOOP;
      PERFORM set_config('app.org_id', '', true), set_config('app.role', '', true);
    END $$
    """,
    "ALTER TABLE bookings ALTER COLUMN booking_number SET NOT NULL",
    "ALTER TABLE bookings ADD CONSTRAINT bookings_org_number_key UNIQUE (org_id, booking_number)",
    f"""
    CREATE FUNCTION assign_booking_number() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.booking_number IS NULL THEN
        INSERT INTO booking_counters AS c (org_id, last_number) VALUES (NEW.org_id, {FIRST_BOOKING_NUMBER})
          ON CONFLICT (org_id) DO UPDATE SET last_number = c.last_number + 1
          RETURNING last_number INTO NEW.booking_number;
      END IF;
      RETURN NEW;
    END $$
    """,
    "CREATE TRIGGER bookings_assign_number BEFORE INSERT ON bookings FOR EACH ROW EXECUTE FUNCTION assign_booking_number()",
]

DOWNGRADE = [
    "DROP TRIGGER bookings_assign_number ON bookings",
    "DROP FUNCTION assign_booking_number()",
    "ALTER TABLE bookings DROP CONSTRAINT bookings_org_number_key",
    "ALTER TABLE bookings DROP COLUMN proposal_id",
    "ALTER TABLE bookings DROP COLUMN booking_number",
    "DROP TABLE booking_counters",
    "ALTER TABLE orgs DROP COLUMN booking_prefix",
]


def upgrade() -> None:
    for statement in UPGRADE:  # one statement per execute: asyncpg prepares each
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
