"""Add durable atomic scheduler leadership lease and task dispatch claims
(LocalClaw branch).

This is the SCHEDULER-CORE CORRECTNESS FIX (classified separately from the
Run Once capability): the old ``system_settings``-based scheduler lock used a
read → decide → unconditional upsert → read-back sequence that could let two
consoles both believe they led a tick.  The new ``scheduler_locks`` row is
acquired atomically via ``INSERT ... ON CONFLICT ... DO UPDATE ... WHERE
until <= now``.  Per-task dispatch admission uses the short-lived
``execution_claim_*`` columns so a scheduler tick and a manual Run Once
cannot both issue create-workstream calls for the same task.

The lease is deliberately bounded (dispatch admission only), NOT a
workstream-lifetime lock: scheduled storage has no authoritative
terminal-state linkage to a node.

Second revision of the LocalClaw manual-run branch.  Revision ID is globally
unique (``lc_scheduler_claims``) and does NOT collide with upstream
070/071.  A future upstream upgrade must merge ``lc_scheduler_claims`` with
the upstream head via an explicit Alembic merge revision.

Revision ID: lc_scheduler_claims
Revises: lc_run_trigger
Create Date: 2026-08-13
"""

import sqlalchemy as sa
from alembic import op

revision = "lc_scheduler_claims"
down_revision = "lc_run_trigger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduler_locks",
        sa.Column("lock_name", sa.Text(), primary_key=True),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("until", sa.Text(), nullable=False),
    )
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(
            sa.Column("execution_claim_id", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("execution_claim_until", sa.Text(), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("execution_claim_until")
        batch_op.drop_column("execution_claim_id")
    op.drop_table("scheduler_locks")
