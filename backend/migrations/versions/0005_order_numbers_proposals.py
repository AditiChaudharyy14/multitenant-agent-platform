"""Order numbers, org order prefix and currency, and proposal guards.

- orgs.order_prefix (e.g. 'HT') and orgs.currency come from org.yaml. The prefix is
  unique, so an order number like HT-1042 names exactly one org (make advance uses it).
- order_counters: one row per org holding the last order number handed out. A BEFORE
  INSERT trigger on orders bumps it with INSERT ... ON CONFLICT DO UPDATE, which takes a
  row lock, so two concurrent orders in one org can never get the same number. It runs
  as the inserting role (SECURITY INVOKER), so RLS on order_counters applies as usual.
- orders.order_number: per-org, starting at 1001, UNIQUE (org_id, order_number).
  Existing orders are numbered by created_at.
- orders.proposal_id UNIQUE: one proposal can create at most one order, so a double
  confirm can't make two orders even if the application check were wrong.
- proposals: at most one open proposal per conversation (partial unique index), the DB
  side of "a new proposal supersedes the older open ones".

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

FIRST_ORDER_NUMBER = 1001

UPGRADE = [
    "ALTER TABLE orgs ADD COLUMN order_prefix text UNIQUE CHECK (order_prefix ~ '^[A-Z]{2,4}$')",
    "ALTER TABLE orgs ADD COLUMN currency text NOT NULL DEFAULT 'NPR' CHECK (currency ~ '^[A-Z]{3}$')",
    """
    CREATE TABLE order_counters (
      org_id       uuid PRIMARY KEY REFERENCES orgs(id),
      last_number  integer NOT NULL CHECK (last_number > 0)
    )
    """,
    "ALTER TABLE order_counters ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE order_counters FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON order_counters USING (org_id = app_org()) WITH CHECK (org_id = app_org())",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON order_counters TO app_user",
    "GRANT SELECT ON order_counters TO app_readonly",
    "ALTER TABLE orders ADD COLUMN order_number integer",
    "ALTER TABLE orders ADD COLUMN proposal_id uuid UNIQUE",
    # Backfill. FORCE RLS applies to app_owner too, so act as each org's admin in turn
    # (set_config is transaction-local and plpgsql re-reads it per statement).
    f"""
    DO $$
    DECLARE o record;
    BEGIN
      FOR o IN SELECT id FROM orgs LOOP
        PERFORM set_config('app.org_id', o.id::text, true), set_config('app.role', 'admin', true);
        UPDATE orders SET order_number = n.num
          FROM (SELECT id, {FIRST_ORDER_NUMBER - 1} + row_number() OVER (ORDER BY created_at, id) AS num
                FROM orders WHERE org_id = o.id) n
          WHERE orders.id = n.id;
        INSERT INTO order_counters (org_id, last_number)
          SELECT o.id, max(order_number) FROM orders WHERE org_id = o.id HAVING count(*) > 0;
      END LOOP;
      PERFORM set_config('app.org_id', '', true), set_config('app.role', '', true);
    END $$
    """,
    "ALTER TABLE orders ALTER COLUMN order_number SET NOT NULL",
    "ALTER TABLE orders ADD CONSTRAINT orders_org_number_key UNIQUE (org_id, order_number)",
    f"""
    CREATE FUNCTION assign_order_number() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.order_number IS NULL THEN
        INSERT INTO order_counters AS c (org_id, last_number) VALUES (NEW.org_id, {FIRST_ORDER_NUMBER})
          ON CONFLICT (org_id) DO UPDATE SET last_number = c.last_number + 1
          RETURNING last_number INTO NEW.order_number;
      END IF;
      RETURN NEW;
    END $$
    """,
    "CREATE TRIGGER orders_assign_number BEFORE INSERT ON orders FOR EACH ROW EXECUTE FUNCTION assign_order_number()",
    "CREATE UNIQUE INDEX proposals_one_open_per_conversation ON proposals (conversation_id) WHERE status = 'open'",
]

DOWNGRADE = [
    "DROP INDEX proposals_one_open_per_conversation",
    "DROP TRIGGER orders_assign_number ON orders",
    "DROP FUNCTION assign_order_number()",
    "ALTER TABLE orders DROP CONSTRAINT orders_org_number_key",
    "ALTER TABLE orders DROP COLUMN proposal_id",
    "ALTER TABLE orders DROP COLUMN order_number",
    "DROP TABLE order_counters",
    "ALTER TABLE orgs DROP COLUMN currency",
    "ALTER TABLE orgs DROP COLUMN order_prefix",
]


def upgrade() -> None:
    for statement in UPGRADE:  # one statement per execute: asyncpg prepares each
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
