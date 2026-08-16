"""Record the provenance of scheduled-task run history.

Existing rows default to ``schedule``.  The authenticated run-now endpoint
records ``manual`` so operators can distinguish an explicit one-shot request
from a due recurring firing without changing the stored schedule definition.

Revision ID: 072
Revises: merge_071_lclaw (LocalClaw adoption — NOT upstream 071)
Create Date: 2026-08-13

LOCALCLAW ADOPTION (2026-08-16): upstream 072 implements the same schema
change already owned by the LocalClaw migration ``lc_run_trigger``
(scheduled_task_runs.trigger TEXT NOT NULL server_default 'schedule').
To avoid duplicate DDL this revision adopts the upstream identity ``072``
with LocalClaw ancestry (``merge_071_lclaw``) and is a NO-OP. The schema
state was created by ``lc_run_trigger``.
"""

import sqlalchemy as sa  # noqa: F401  (kept for parity with upstream file shape)
from alembic import op  # noqa: F401

revision = "072"
down_revision = "merge_071_lclaw"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op: scheduled_task_runs.trigger was created by lc_run_trigger.
    pass


def downgrade() -> None:
    # No-op: do not drop the column; lc_run_trigger owns its lifecycle.
    pass
