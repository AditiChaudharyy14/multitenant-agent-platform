"""Chat: message status, org business type, agent_runs turn id and error.

- messages.status: 'stopped' marks a partial answer the user stopped (or whose stream was
  cut), 'error' one that ended on a provider failure. Both keep whatever was produced.
- orgs.business_type: one line for the system prompt ("online tea shop ..."), from org.yaml.
- agent_runs.turn_id links a run to the user message that started it; error holds the
  failure reason. status is now constrained to the values the loop writes.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-06
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE messages ADD COLUMN status text NOT NULL DEFAULT 'complete' "
        "CHECK (status IN ('complete','stopped','error'))"
    )
    op.execute("ALTER TABLE orgs ADD COLUMN business_type text NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE agent_runs ADD COLUMN turn_id uuid, ADD COLUMN error text")
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check "
        "CHECK (status IN ('ok','error','stopped','rate_limited'))"
    )
    # History loads a conversation's latest messages; this keeps that an index scan.
    op.execute("CREATE INDEX messages_conversation_created_idx ON messages (conversation_id, created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX messages_conversation_created_idx")
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check")
    op.execute("ALTER TABLE agent_runs DROP COLUMN error, DROP COLUMN turn_id")
    op.execute("ALTER TABLE orgs DROP COLUMN business_type")
    op.execute("ALTER TABLE messages DROP COLUMN status")
