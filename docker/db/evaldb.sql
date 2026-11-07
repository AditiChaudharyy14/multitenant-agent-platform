-- Eval databases on db-ephemeral only (never the dev `db`). Runs at db-ephemeral's init and
-- again, idempotently, from `make evaldb`. Run as the bootstrap superuser, like testdb.sql,
-- because creating roles and installing pgvector need one.
--
-- eval_template: migrated and seeded (frozen SEED_NOW) by the eval runner as app_owner.
--   Marked IS_TEMPLATE, so any role with CREATEDB may clone it.
-- app_eval: may only create and drop databases (one clone per scenario). It owns no
--   tables and is not a member of any app role, so it cannot read tenant data, and
--   app_owner keeps NOCREATEDB. No superuser credentials ever leave this container.

\getenv eval_pw APP_EVAL_PASSWORD
SET client_min_messages = warning;

SELECT 'CREATE ROLE app_eval LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT CREATEDB'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_eval')\gexec
-- Re-applied every run so a changed APP_EVAL_PASSWORD takes effect.
ALTER ROLE app_eval WITH LOGIN NOSUPERUSER NOBYPASSRLS CREATEDB PASSWORD :'eval_pw';

SELECT 'CREATE DATABASE eval_template OWNER app_owner'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'eval_template')\gexec
ALTER DATABASE eval_template IS_TEMPLATE true;

\connect eval_template
SET client_min_messages = warning;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;
-- As in init.sql/testdb.sql: rls_definer must be able to own busy_intervals().
GRANT CREATE ON SCHEMA public TO rls_definer;
