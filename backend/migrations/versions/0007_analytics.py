"""Analytics: stock_events, and admin conversations guarded in the database.

- stock_events: one row each time a customer asked for more units than were in stock
  (create_order, update_order, reorder). Customer-owned like orders: org_id + user_id,
  ENABLE + FORCE RLS, tenant_isolation and the RESTRICTIVE own_rows policy. Feeds the
  admin metric most_out_of_stock. app_readonly may SELECT it, nothing more.
- admin_kind: a RESTRICTIVE policy on conversations. A conversation of kind 'admin' (the
  analytics chat) is invisible to, and can't be created or produced by UPDATE in, any
  context whose app.role isn't admin. The API already requires role admin for those
  routes; this makes an app bug unable to hand a customer an admin chat.
- Indexes for the metric windows (created_at ranges per org).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-07
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

UPGRADE = [
    """
    CREATE TABLE stock_events (
      id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id           uuid NOT NULL REFERENCES orgs(id),
      user_id          uuid NOT NULL,
      sku              text NOT NULL,
      requested_qty    integer NOT NULL CHECK (requested_qty > 0),
      available_qty    integer NOT NULL CHECK (available_qty >= 0 AND available_qty < requested_qty),
      conversation_id  uuid,
      created_at       timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (org_id, user_id) REFERENCES users(org_id, id)
    )
    """,
    "CREATE INDEX stock_events_org_created_idx ON stock_events (org_id, created_at)",
    "ALTER TABLE stock_events ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE stock_events FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON stock_events USING (org_id = app_org()) WITH CHECK (org_id = app_org())",
    """
    CREATE POLICY own_rows ON stock_events AS RESTRICTIVE
      USING (app_role() = 'admin' OR user_id = app_uid())
      WITH CHECK (app_role() = 'admin' OR user_id = app_uid())
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON stock_events TO app_user",
    "GRANT SELECT ON stock_events TO app_readonly",
    """
    CREATE POLICY admin_kind ON conversations AS RESTRICTIVE
      USING (kind = 'customer' OR app_role() = 'admin')
      WITH CHECK (kind = 'customer' OR app_role() = 'admin')
    """,
    "CREATE INDEX conversations_org_kind_created_idx ON conversations (org_id, kind, created_at)",
    "CREATE INDEX orders_org_created_idx ON orders (org_id, created_at)",
    "CREATE INDEX bookings_org_starts_idx ON bookings (org_id, starts_at)",
    "CREATE INDEX agent_runs_org_created_idx ON agent_runs (org_id, created_at)",
]

DOWNGRADE = [
    "DROP INDEX agent_runs_org_created_idx",
    "DROP INDEX bookings_org_starts_idx",
    "DROP INDEX orders_org_created_idx",
    "DROP INDEX conversations_org_kind_created_idx",
    "DROP POLICY admin_kind ON conversations",
    "DROP TABLE stock_events",
]


def upgrade() -> None:
    for statement in UPGRADE:  # one statement per execute: asyncpg prepares each
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
