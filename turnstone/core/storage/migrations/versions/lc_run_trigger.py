"""Record the provenance of scheduled-task run history (LocalClaw branch).

Existing rows default to ``schedule``.  The authenticated run-now endpoint
records ``manual`` so operators can distinguish an explicit one-shot request
from a due recurring firing without changing the stored schedule definition.

This is the first revision of the LocalClaw manual-run branch, forked from the
exact 1.8.0a6 head (069).  Revision ID is globally unique (``lc_run_trigger``)
and deliberately does NOT collide with upstream migration numbers 070/071
(``070_model_max_concurrency``, ``071_conversations_commit_key``) which belong
to the separate post-1.8.0a6 upstream lineage.  A future upgrade to upstream
must join the two heads with an explicit Alembic merge revision (see the
migration-lineage gate package).

Revision ID: lc_run_trigger
Revises: 069
Create Date: 2026-08-13
"""

import sqlalchemy as sa
from alembic import op

revision = "lc_run_trigger"
down_revision = "069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scheduled_task_runs") as batch_op:
        batch_op.add_column(
            sa.Column("trigger", sa.Text(), nullable=False, server_default="schedule")
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_task_runs") as batch_op:
        batch_op.drop_column("trigger")
