-- schema_reference.sql
-- Reference DDL for the first Alembic migration. Claude Code: port this into
-- backend/migrations as raw op.execute() SQL. Keep every RLS policy and constraint.

-- ───────────── Extensions ─────────────
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;

-- ───────────── Roles (created by docker/db/init.sql, shown here for reference) ─────────────
-- CREATE ROLE app_owner    LOGIN PASSWORD :'owner_pw';          -- runs migrations, owns tables
-- CREATE ROLE app_user     LOGIN PASSWORD :'user_pw' NOBYPASSRLS; -- runtime
-- CREATE ROLE app_readonly LOGIN PASSWORD :'ro_pw'   NOBYPASSRLS; -- admin analytics
-- CREATE ROLE rls_definer  NOLOGIN BYPASSRLS;                     -- owns busy_intervals() only

-- ───────────── Helper: current tenant settings ─────────────
CREATE FUNCTION app_org()  RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.org_id',  true), '')::uuid $$;
CREATE FUNCTION app_uid()  RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;
CREATE FUNCTION app_role() RETURNS text LANGUAGE sql STABLE AS
  $$ SELECT COALESCE(NULLIF(current_setting('app.role', true), ''), 'none') $$;

-- ───────────── Global (no RLS) ─────────────
CREATE TABLE orgs (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug      text UNIQUE NOT NULL,
  name      text NOT NULL,
  vertical  text NOT NULL CHECK (vertical IN ('store','booking')),
  timezone  text NOT NULL DEFAULT 'Asia/Kathmandu',
  branding  jsonb NOT NULL DEFAULT '{}'
);

-- ───────────── Users & sessions ─────────────
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

-- ───────────── Org A: store ─────────────
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
  FOREIGN KEY (org_id, order_id)   REFERENCES orders(org_id, id) ON DELETE CASCADE,
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
  FOREIGN KEY (org_id, order_id) REFERENCES orders(org_id, id) ON DELETE CASCADE
);

-- ───────────── Org B: booking ─────────────
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

-- ───────────── Shared: chat, proposals, runs, KB ─────────────
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
  FOREIGN KEY (org_id, conversation_id) REFERENCES conversations(org_id, id) ON DELETE CASCADE
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
  FOREIGN KEY (org_id, conversation_id) REFERENCES conversations(org_id, id) ON DELETE CASCADE
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

-- ───────────── RLS: tenant policy on every tenant table ─────────────
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

-- ───────────── RLS: customers see only their own rows (RESTRICTIVE = AND-ed) ─────────────
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

-- ───────────── Availability without leaking other customers' bookings ─────────────
-- Owned by rls_definer (BYPASSRLS), so it can see every booking — but it filters by the
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
ALTER FUNCTION busy_intervals(timestamptz, timestamptz) OWNER TO rls_definer;
REVOKE ALL ON FUNCTION busy_intervals(timestamptz, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION busy_intervals(timestamptz, timestamptz) TO app_user;

-- ───────────── Grants ─────────────
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
