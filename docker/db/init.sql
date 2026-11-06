-- Runs once, as the bootstrap superuser, when the data volume is empty.
-- The image's entrypoint runs this file through psql with ON_ERROR_STOP=1.
-- Passwords come from the container environment via \getenv.

\getenv db_name POSTGRES_DB
\getenv owner_pw APP_OWNER_PASSWORD
\getenv user_pw APP_USER_PASSWORD
\getenv ro_pw APP_READONLY_PASSWORD

CREATE ROLE app_owner    LOGIN PASSWORD :'owner_pw' NOSUPERUSER NOBYPASSRLS;  -- migrations, seed; owns tables
CREATE ROLE app_user     LOGIN PASSWORD :'user_pw'  NOSUPERUSER NOBYPASSRLS;  -- runtime
CREATE ROLE app_readonly LOGIN PASSWORD :'ro_pw'    NOSUPERUSER NOBYPASSRLS;  -- admin analytics
CREATE ROLE rls_definer  NOLOGIN NOSUPERUSER BYPASSRLS;                       -- owns busy_intervals() only

-- On PG15+ the public schema belongs to pg_database_owner, so this also lets
-- app_owner create objects there.
ALTER DATABASE :"db_name" OWNER TO app_owner;

-- Installed here because creating pgvector requires a superuser. The migration's
-- CREATE EXTENSION IF NOT EXISTS statements are then no-ops when run as app_owner.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;

-- Both are required for app_owner to run ALTER FUNCTION busy_intervals ... OWNER TO
-- rls_definer. INHERIT FALSE means app_owner holds none of rls_definer's privileges
-- unless it explicitly switches role.
GRANT rls_definer TO app_owner WITH INHERIT FALSE, SET TRUE;
GRANT CREATE ON SCHEMA public TO rls_definer;
