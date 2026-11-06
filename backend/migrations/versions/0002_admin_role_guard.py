"""Guard admin creation: only an admin context (or app_owner) may create or promote one.

Signup and login run in anon_tx (app.role unset → app_role() = 'none'). The app always
inserts role 'customer', but this makes that a database guarantee: a bug that passes
role='admin' through fails here. It also closes a gap in own_user_row, which lets a
customer UPDATE their own users row and so could have set role = 'admin'.

A trigger rather than a RESTRICTIVE policy, because a policy can't see OLD: the login
rehash (UPDATE password_hash on an existing admin, in anon_tx) must keep working.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

GUARD_FUNCTION = """
CREATE FUNCTION users_admin_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- Only a new admin row, or a role change to admin, is checked.
  IF NEW.role = 'admin'
     AND (TG_OP = 'INSERT' OR OLD.role IS DISTINCT FROM 'admin')
     AND app_role() <> 'admin'
     AND current_user <> 'app_owner'   -- SECURITY INVOKER: the connected role
  THEN
    RAISE EXCEPTION 'only an admin context may create or promote an admin user'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$
"""

GUARD_TRIGGER = """
CREATE TRIGGER users_admin_guard
  BEFORE INSERT OR UPDATE OF role ON users
  FOR EACH ROW EXECUTE FUNCTION users_admin_guard()
"""


def upgrade() -> None:
    op.execute(GUARD_FUNCTION)
    op.execute(GUARD_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER users_admin_guard ON users")
    op.execute("DROP FUNCTION users_admin_guard()")
