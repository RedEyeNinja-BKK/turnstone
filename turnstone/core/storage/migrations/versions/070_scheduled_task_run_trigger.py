"""Record the provenance of scheduled-task run history.

Existing rows default to ``schedule``.  The authenticated run-now endpoint
records ``manual`` so operators can distinguish an explicit one-shot request
from a due recurring firing without changing the stored schedule definition.

Revision ID: 070
Revises: 069
Create Date: 2026-08-13
"""

import sqlalchemy as sa
from alembic import op

revision = "070"
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
