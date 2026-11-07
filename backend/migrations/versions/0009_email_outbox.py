"""Email notifications: a transactional outbox, and booking reminder bookkeeping.

- email_outbox: one row per email to send, written in the SAME transaction as the change
  that triggers it (order shipped, booking confirmed, booking reminder), so an email is
  queued if and only if the change committed. dedupe_key (unique per org) makes every
  trigger idempotent. A background sender delivers pending rows over SMTP (Mailpit in
  development) and marks them sent, backing off on failure.
  Tenant table: org_id, ENABLE + FORCE RLS, tenant_isolation. Reading, updating and
  deleting are admin-only (RESTRICTIVE, per command): the sender runs admin-scoped per
  org, and a customer can't read even their own queued email. A customer-scoped
  transaction (a booking confirmed by the customer) may INSERT only a row addressed to
  themselves (user_id = app_uid()).
- bookings.reminder_sent_at: set when the 24-hour reminder is queued; reset when the
  booking is moved, so the new time gets its own reminder.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-07
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

UPGRADE = [
    """
    CREATE TABLE email_outbox (
      id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id           uuid NOT NULL REFERENCES orgs(id),
      user_id          uuid NOT NULL,
      kind             text NOT NULL CHECK (kind IN ('order_shipped', 'booking_confirmed', 'booking_reminder')),
      to_email         text NOT NULL,
      subject          text NOT NULL,
      body_text        text NOT NULL,
      body_html        text NOT NULL,
      dedupe_key       text NOT NULL,
      status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
      attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
      last_error       text,
      next_attempt_at  timestamptz NOT NULL DEFAULT now(),
      sent_at          timestamptz,
      created_at       timestamptz NOT NULL DEFAULT now(),
      UNIQUE (org_id, dedupe_key),
      FOREIGN KEY (org_id, user_id) REFERENCES users(org_id, id)
    )
    """,
    "CREATE INDEX email_outbox_pending_idx ON email_outbox (org_id, next_attempt_at) WHERE status = 'pending'",
    "ALTER TABLE email_outbox ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE email_outbox FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON email_outbox USING (org_id = app_org()) WITH CHECK (org_id = app_org())",
    "CREATE POLICY admin_reads ON email_outbox AS RESTRICTIVE FOR SELECT USING (app_role() = 'admin')",
    "CREATE POLICY admin_updates ON email_outbox AS RESTRICTIVE FOR UPDATE USING (app_role() = 'admin')",
    "CREATE POLICY admin_deletes ON email_outbox AS RESTRICTIVE FOR DELETE USING (app_role() = 'admin')",
    """
    CREATE POLICY own_inserts ON email_outbox AS RESTRICTIVE FOR INSERT
      WITH CHECK (app_role() = 'admin' OR user_id = app_uid())
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON email_outbox TO app_user",
    "ALTER TABLE bookings ADD COLUMN reminder_sent_at timestamptz",
    """
    CREATE INDEX bookings_reminder_due_idx ON bookings (org_id, starts_at)
      WHERE status = 'confirmed' AND reminder_sent_at IS NULL
    """,
]

DOWNGRADE = [
    "DROP INDEX bookings_reminder_due_idx",
    "ALTER TABLE bookings DROP COLUMN reminder_sent_at",
    "DROP TABLE email_outbox",
]


def upgrade() -> None:
    for statement in UPGRADE:  # one statement per execute: asyncpg prepares each
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
