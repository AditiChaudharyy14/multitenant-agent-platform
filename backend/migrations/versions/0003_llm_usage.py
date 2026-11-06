"""Per-model daily LLM request counter (llm_usage).

Global, not tenant data: the vendor quota belongs to the API key, so the table has no
org_id and no RLS. app_user may read and count; nothing else touches it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE llm_usage (
          provider   text NOT NULL,
          model      text NOT NULL,
          day        date NOT NULL,                   -- in the vendor's quota timezone (Pacific)
          requests   integer NOT NULL CHECK (requests >= 0),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (provider, model, day)
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON llm_usage TO app_user")


def downgrade() -> None:
    op.execute("DROP TABLE llm_usage")
