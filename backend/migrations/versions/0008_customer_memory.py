"""Long-term customer memory, and per-org feature flags.

- customer_memory: what a customer asked the assistant to remember (source 'stated'), plus
  tombstones for derived facts they asked it to forget (source 'derived', value NULL).
  Derived facts themselves ("usual" products, practitioner, service) are computed live
  from the customer's own orders/bookings and never stored. Customer-owned like orders:
  org_id + user_id, ENABLE + FORCE RLS, tenant_isolation and the RESTRICTIVE own_rows
  policy (invariant 2). app_readonly gets no grant: the admin analytics agent never sees it.
- orgs.features: the org.yaml `features` block (e.g. {"memory": true}), synced like branding.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-07
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE orgs ADD COLUMN features jsonb NOT NULL DEFAULT '{}'::jsonb",
    """
    CREATE TABLE customer_memory (
      id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id      uuid NOT NULL REFERENCES orgs(id),
      user_id     uuid NOT NULL,
      key         text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{0,39}$'),
      value       text CHECK (value IS NULL OR char_length(value) BETWEEN 1 AND 200),
      source      text NOT NULL CHECK (source IN ('stated', 'derived')),
      updated_at  timestamptz NOT NULL DEFAULT now(),
      -- A stated memory always has a value; a derived row is only ever a tombstone.
      CHECK ((source = 'stated') = (value IS NOT NULL)),
      UNIQUE (org_id, user_id, key),
      FOREIGN KEY (org_id, user_id) REFERENCES users(org_id, id)
    )
    """,
    "ALTER TABLE customer_memory ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE customer_memory FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON customer_memory USING (org_id = app_org()) WITH CHECK (org_id = app_org())",
    """
    CREATE POLICY own_rows ON customer_memory AS RESTRICTIVE
      USING (app_role() = 'admin' OR user_id = app_uid())
      WITH CHECK (app_role() = 'admin' OR user_id = app_uid())
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON customer_memory TO app_user",
]

DOWNGRADE = [
    "DROP TABLE customer_memory",
    "ALTER TABLE orgs DROP COLUMN features",
]


def upgrade() -> None:
    for statement in UPGRADE:  # one statement per execute: asyncpg prepares each
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
