-- Creates the app_test database used by `make test`, on db-ephemeral only, so tests never
-- touch the dev `db`. Runs at db-ephemeral's init and again (idempotently) from
-- `make testdb`. Run as the bootstrap superuser, because app_owner
-- has no CREATEDB and pgvector can only be installed by a superuser.
-- The roles are cluster-wide, so they are the same ones init.sql created.

SELECT 'CREATE DATABASE app_test OWNER app_owner'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'app_test')\gexec

\connect app_test
-- Hide the "already exists, skipping" notices on repeat runs.
SET client_min_messages = warning;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;

-- Schema privileges are per database, so this repeats init.sql's grant: rls_definer
-- needs CREATE on the schema to become the owner of busy_intervals().
GRANT CREATE ON SCHEMA public TO rls_definer;
