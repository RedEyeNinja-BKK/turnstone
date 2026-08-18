"""LocalClaw scheduler lineage: re-identified historical "072" adoption.

This is the LocalClaw fork's replacement for the revision previously named
``072_scheduled_task_run_trigger`` (revision ID ``072``, in the LocalClaw
1.8.0a7 fork branch).  That revision deliberately adopted the upstream
revision ID ``072`` as a LOCALCLAW NO-OP because the schema change it nominally
carried (``scheduled_task_runs.trigger``) was already created by the LocalClaw
migration ``lc_run_trigger``.

In the stable-line graft, the upstream revision ``072`` is taken by the
genuine upstream migration ``072_memory_index_snapshots`` (which performs
real DDL: the ``memory_index_snapshots`` table, the ``intent_verdicts``
principal witness columns, etc.).  A revision ID is a global graph identity,
so the old LocalClaw no-op CANNOT also be named ``072`` without colliding.

This file therefore re-identifies that already-applied LocalClaw adoption
under a globally unique revision ID.  It remains a NO-OP: the schema it
represents is already present in production (created by ``lc_run_trigger``),
and (as with the old ``072_scheduled_task_run_trigger``) nothing is run here.
Its sole purpose is to give the already-applied LocalClaw state a stable,
unambiguous Alembic identity in the reconciled downstream graph.

Schema semantics are EXACTLY those of the historical LocalClaw
``072_scheduled_task_run_trigger``: no DDL of its own; it marks that the
LocalClaw scheduler branch (``lc_run_trigger`` -> ``lc_scheduler_claims`` ->
``merge_071_lclaw``) has reached its post-adoption terminal state.

Revision ID: 42e931fce0d1
Revises: merge_071_lclaw
Create Date: 2026-08-19 (LocalClaw stable-line graft)
"""

import sqlalchemy as sa  # noqa: F401  (kept for parity with upstream shape)
from alembic import op  # noqa: F401

revision = "42e931fce0d1"
down_revision = "merge_071_lclaw"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op: scheduled_task_runs.trigger was created by lc_run_trigger.
    pass


def downgrade() -> None:
    # No-op: do not drop the column; lc_run_trigger owns its lifecycle.
    pass
