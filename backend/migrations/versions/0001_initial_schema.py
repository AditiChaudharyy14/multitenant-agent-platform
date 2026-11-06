"""Initial schema: tables, RLS policies, grants and busy_intervals().

Ported from schema_reference.sql as raw SQL. Two additions beyond the reference are
marked HARDENING; both only add restrictions.

Runs as app_owner. Prerequisites from docker/db/init.sql (and docker/db/testdb.sql):
the extensions are installed by the superuser, app_owner may SET ROLE rls_definer, and
rls_definer has CREATE on schema public, which ALTER FUNCTION ... OWNER TO requires.

Revision ID: 0001
Revises:
Create Date: 2026-11-06
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "users", "products", "orders", "order_items", "order_status_history",
    "services", "practitioners", "practitioner_services", "opening_hours", "holidays",
    "bookings", "conversations", "messages", "proposals", "agent_runs", "kb_chunks",
)  # fmt: skip

EXTENSIONS = """
-- No-ops here (already installed by the superuser); kept so the schema is self-describing.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;
"""

HELPERS = """
CREATE FUNCTION app_org()  RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.org_id',  true), '')::uuid $$;
CREATE FUNCTION app_uid()  RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;
CREATE FUNCTION app_role() RETURNS text LANGUAGE sql STABLE AS
  $$ SELECT COALESCE(NULLIF(current_setting('app.role', true), ''), 'none') $$;
"""

GLOBAL_AND_USERS = """
CREATE TABLE orgs (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug      text UNIQUE NOT NULL,
  name      text NOT NULL,
  vertical  text NOT NULL CHECK (vertical IN ('store','booking')),
  timezone  text NOT NULL DEFAULT 'Asia/Kathmandu',
  branding  jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        uuid NOT NULL REFERENCES orgs(id),
  email         citext NOT NULL,
  display_name  text NOT NULL,
  password_hash text NOT NULL,                        -- argon2id
  role          text NOT NULL CHECK (role IN ('customer','admin')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, email),
  UNIQUE (org_id, id)                                 -- target for composite FKs
);

CREATE TABLE sessions (                               -- NO RLS: looked up by token hash only
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash  bytea UNIQUE NOT NULL,                  -- sha256(raw token); raw token only in cookie
  user_id     uuid NOT NULL,
  org_id      uuid NOT NULL,
  role        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  FOREIGN KEY (org_id, user_id) REFERENCES users(org_id, id) ON DELETE CASCADE
);
"""

STORE = """
CREATE TABLE products (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id    uuid NOT NULL REFERENCES orgs(id),
  sku       text NOT NULL,
  name      text NOT NULL,
  category  text NOT NULL,
  price     numeric(10,2) NOT NULL CHECK (price >= 0),
  stock     integer NOT NULL CHECK (stock >= 0),      -- second guard behind the atomic UPDATE
  UNIQUE (org_id, sku),
  UNIQUE (org_id, id)
);

CREATE TABLE orders (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id           uuid NOT NULL,
  user_id          uuid NOT NULL,
  status           text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','confirmed','shipped','delivered','cancelled')),
  delivery_address text NOT NULL,
  payment_method   text NOT NULL DEFAULT 'cod',
  total            numeric(10,2) NOT NULL,
  conversation_id  uuid,                              -- conversion analytics
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id),
  -- HARDENING: target for the owner-consistent FKs from order_items/order_status_history.
  UNIQUE (org_id, user_id, id),
  FOREIGN KEY (org_id, user_id) REFERENCES users(org_id, id)
);

CREATE TABLE order_items (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      uuid NOT NULL,
  user_id     uuid NOT NULL,                          -- denormalised for the restrictive policy
  order_id    uuid NOT NULL,
  product_id  uuid NOT NULL,
  sku         text NOT NULL,
  name        text NOT NULL,                          -- snapshot
  unit_price  numeric(10,2) NOT NULL,                 -- snapshot at order time
  qty         integer NOT NULL CHECK (qty > 0),
  -- HARDENING: was (org_id, order_id). Including user_id means the denormalised owner must
  -- match the order's owner; FK checks bypass RLS, so the policy alone can't ensure that.
  FOREIGN KEY (org_id, user_id, order_id) REFERENCES orders(org_id, user_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, product_id) REFERENCES products(org_id, id)
);

CREATE TABLE order_status_history (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      uuid NOT NULL,
  user_id     uuid NOT NULL,
  order_id    uuid NOT NULL,
  from_status text,
  to_status   text NOT NULL,
  changed_at  timestamptz NOT NULL DEFAULT now(),
  changed_by  text NOT NULL,                          -- 'customer' | 'admin' | 'system'
  -- HARDENING: was (org_id, order_id); see order_items.
  FOREIGN KEY (org_id, user_id, order_id) REFERENCES orders(org_id, user_id, id) ON DELETE CASCADE
);
"""

BOOKING = """
CREATE TABLE services (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        uuid NOT NULL REFERENCES orgs(id),
  name          text NOT NULL,
  duration_min  integer NOT NULL CHECK (duration_min > 0 AND duration_min % 15 = 0),
  price         numeric(10,2) NOT NULL CHECK (price >= 0),
  UNIQUE (org_id, name),
  UNIQUE (org_id, id)
);

CREATE TABLE practitioners (
  id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id  uuid NOT NULL REFERENCES orgs(id),
  name    text NOT NULL,
  UNIQUE (org_id, id)
);

CREATE TABLE practitioner_services (
  org_id          uuid NOT NULL,
  practitioner_id uuid NOT NULL,
  service_id      uuid NOT NULL,
  PRIMARY KEY (practitioner_id, service_id),
  FOREIGN KEY (org_id, practitioner_id) REFERENCES practitioners(org_id, id),
  FOREIGN KEY (org_id, service_id)      REFERENCES services(org_id, id)
);

CREATE TABLE opening_hours (
  org_id   uuid NOT NULL REFERENCES orgs(id),
  weekday  smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),   -- 0 = Monday (ISO-1)
  opens    time NOT NULL,
  closes   time NOT NULL CHECK (closes > opens),                -- local org time
  PRIMARY KEY (org_id, weekday)                                 -- absent row = closed
);

CREATE TABLE holidays (
  org_id  uuid NOT NULL REFERENCES orgs(id),
  day     date NOT NULL,                                        -- local org date
  name    text NOT NULL,
  PRIMARY KEY (org_id, day)
);

CREATE TABLE bookings (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id           uuid NOT NULL,
  user_id          uuid NOT NULL,
  service_id       uuid NOT NULL,
  practitioner_id  uuid NOT NULL,
  starts_at        timestamptz NOT NULL,
  ends_at          timestamptz NOT NULL,
  status           text NOT NULL DEFAULT 'confirmed'
                   CHECK (status IN ('confirmed','cancelled','completed')),
  price            numeric(10,2) NOT NULL,
  cancellation_fee numeric(10,2),
  conversation_id  uuid,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (ends_at > starts_at),
  UNIQUE (org_id, id),
  FOREIGN KEY (org_id, user_id)         REFERENCES users(org_id, id),
  FOREIGN KEY (org_id, service_id)      REFERENCES services(org_id, id),
  FOREIGN KEY (org_id, practitioner_id) REFERENCES practitioners(org_id, id),
  -- The concurrency guarantee: the DB makes double-booking impossible.
  CONSTRAINT no_practitioner_overlap EXCLUDE USING gist
    (practitioner_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&)
    WHERE (status = 'confirmed'),
  CONSTRAINT no_customer_overlap EXCLUDE USING gist
    (user_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&)
    WHERE (status = 'confirmed')
);
"""

SHARED = """
CREATE TABLE conversations (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      uuid NOT NULL,
  user_id     uuid NOT NULL,
  kind        text NOT NULL DEFAULT 'customer' CHECK (kind IN ('customer','admin')),
  title       text NOT NULL DEFAULT 'New chat',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  deleted_at  timestamptz,
  UNIQUE (org_id, id),
  -- HARDENING: target for the owner-consistent FKs from messages/proposals.
  UNIQUE (org_id, user_id, id),
  FOREIGN KEY (org_id, user_id) REFERENCES users(org_id, id)
);

CREATE TABLE messages (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id           uuid NOT NULL,
  user_id          uuid NOT NULL,
  conversation_id  uuid NOT NULL,
  role             text NOT NULL CHECK (role IN ('user','assistant','tool')),
  content          text NOT NULL DEFAULT '',
  tool_calls       jsonb,
  citations        jsonb,
  cards            jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  -- HARDENING: was (org_id, conversation_id); see order_items.
  FOREIGN KEY (org_id, user_id, conversation_id)
    REFERENCES conversations(org_id, user_id, id) ON DELETE CASCADE
);

CREATE TABLE proposals (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id           uuid NOT NULL,
  user_id          uuid NOT NULL,
  conversation_id  uuid NOT NULL,
  kind             text NOT NULL,      -- create_order|update_order|cancel_order|reorder|create_booking|reschedule_booking|cancel_booking
  payload          jsonb NOT NULL,     -- exactly what will be committed
  summary          jsonb NOT NULL,     -- what the customer was shown (items, prices, total / slot)
  status           text NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','confirmed','superseded','expired','rejected')),
  created_turn_id  uuid NOT NULL,      -- user message id of the turn that created it
  expires_at       timestamptz NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  -- HARDENING: was (org_id, conversation_id); see order_items.
  FOREIGN KEY (org_id, user_id, conversation_id)
    REFERENCES conversations(org_id, user_id, id) ON DELETE CASCADE
);

CREATE TABLE agent_runs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id           uuid NOT NULL REFERENCES orgs(id),
  user_id          uuid NOT NULL,
  conversation_id  uuid NOT NULL,
  provider         text NOT NULL,
  model            text NOT NULL,
  input_tokens     integer NOT NULL DEFAULT 0,
  output_tokens    integer NOT NULL DEFAULT 0,
  latency_ms       integer,
  tools_called     jsonb NOT NULL DEFAULT '[]',
  status           text NOT NULL,      -- ok|error|stopped|rate_limited
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kb_chunks (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id     uuid NOT NULL REFERENCES orgs(id),
  doc_id     text NOT NULL,
  doc_title  text NOT NULL,
  section    text NOT NULL,
  content    text NOT NULL,
  embedding  vector(384) NOT NULL,
  tsv        tsvector GENERATED ALWAYS AS (to_tsvector('english', doc_title || ' ' || section || ' ' || content)) STORED,
  UNIQUE (org_id, doc_id, section)
);
CREATE INDEX kb_chunks_embedding_idx ON kb_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX kb_chunks_tsv_idx       ON kb_chunks USING gin (tsv);
CREATE INDEX kb_chunks_org_idx       ON kb_chunks (org_id);
"""

TENANT_POLICIES = """
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users','products','orders','order_items','order_status_history',
    'services','practitioners','practitioner_services','opening_hours','holidays',
    'bookings','conversations','messages','proposals','agent_runs','kb_chunks'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (org_id = app_org()) WITH CHECK (org_id = app_org())', t);
  END LOOP;
END $$;
"""

OWN_ROWS_POLICIES = """
-- RESTRICTIVE policies are AND-ed with tenant_isolation: customers see only their own rows.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'orders','order_items','order_status_history','bookings',
    'conversations','messages','proposals'
  ] LOOP
    EXECUTE format(
      'CREATE POLICY own_rows ON %I AS RESTRICTIVE
         USING (app_role() = ''admin'' OR user_id = app_uid())
         WITH CHECK (app_role() = ''admin'' OR user_id = app_uid())', t);
  END LOOP;
END $$;

-- HARDENING: a customer sees only their own users row (no other emails or password hashes).
-- Admins see the whole org; so does the anonymous login/signup context (role 'none'),
-- which must look an email up before anyone is authenticated.
CREATE POLICY own_user_row ON users AS RESTRICTIVE
  USING (app_role() <> 'customer' OR id = app_uid())
  WITH CHECK (app_role() <> 'customer' OR id = app_uid());
"""

BUSY_INTERVALS = """
-- Owned by rls_definer (BYPASSRLS), so it can see every booking, but it filters by the
-- caller's org explicitly and returns no customer data.
CREATE FUNCTION busy_intervals(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (practitioner_id uuid, starts_at timestamptz, ends_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT b.practitioner_id, b.starts_at, b.ends_at
  FROM bookings b
  WHERE b.org_id = app_org()
    AND app_org() IS NOT NULL
    AND b.status = 'confirmed'
    AND tstzrange(b.starts_at, b.ends_at, '[)') && tstzrange(p_from, p_to, '[)')
$$;
-- Privileges first, ownership last (the reference had the opposite order). Once the owner is
-- rls_definer, app_owner (a member WITH INHERIT FALSE) may no longer grant or revoke, and
-- Postgres would skip both with only a warning, leaving EXECUTE open to PUBLIC.
-- ALTER ... OWNER carries the ACL over to the new owner.
REVOKE ALL ON FUNCTION busy_intervals(timestamptz, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION busy_intervals(timestamptz, timestamptz) TO app_user;
ALTER FUNCTION busy_intervals(timestamptz, timestamptz) OWNER TO rls_definer;
"""

GRANTS = """
GRANT USAGE ON SCHEMA public TO app_user, app_readonly;
GRANT SELECT ON orgs TO app_user, app_readonly;
GRANT SELECT, INSERT, DELETE ON sessions TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON
  users, products, orders, order_items, order_status_history,
  services, practitioners, practitioner_services, opening_hours, holidays,
  bookings, conversations, messages, proposals, agent_runs, kb_chunks
TO app_user;
GRANT SELECT ON
  users, products, orders, order_items, order_status_history,
  services, practitioners, practitioner_services, opening_hours, holidays,
  bookings, conversations, messages, agent_runs
TO app_readonly;
GRANT SELECT ON bookings TO rls_definer;
GRANT EXECUTE ON FUNCTION app_org(), app_uid(), app_role() TO app_user, app_readonly, rls_definer;
"""


def statements(block: str) -> list[str]:
    """Split a block into single statements.

    asyncpg runs each op.execute() as a prepared statement, which may hold only one
    command. A statement ends at a line ending in ';' outside a $$ ... $$ body; an odd
    number of '$$' on a line opens or closes a body.
    """
    out: list[str] = []
    current: list[str] = []
    in_body = False
    for line in block.splitlines():
        current.append(line)
        if line.count("$$") % 2:
            in_body = not in_body
        if not in_body and line.rstrip().endswith(";"):
            out.append("\n".join(current).strip())
            current = []
    assert not "\n".join(current).strip(), "trailing SQL without ';'"
    return out


def upgrade() -> None:
    # Alembic wraps all of this in one transaction, so a failure leaves nothing behind.
    for block in (
        EXTENSIONS, HELPERS, GLOBAL_AND_USERS, STORE, BOOKING, SHARED,
        TENANT_POLICIES, OWN_ROWS_POLICIES, BUSY_INTERVALS, GRANTS,
    ):  # fmt: skip
        for statement in statements(block):
            op.execute(statement)


def downgrade() -> None:
    # Dropping a table drops its policies, indexes and constraints with it. The extensions
    # stay: they belong to the superuser and the database was created with them.
    op.execute("DROP FUNCTION busy_intervals(timestamptz, timestamptz)")
    op.execute(
        """
        DROP TABLE kb_chunks, agent_runs, proposals, messages, conversations,
                   bookings, holidays, opening_hours, practitioner_services,
                   practitioners, services, order_status_history, order_items,
                   orders, products, sessions, users, orgs
        """
    )
    op.execute("DROP FUNCTION app_role(), app_uid(), app_org()")
