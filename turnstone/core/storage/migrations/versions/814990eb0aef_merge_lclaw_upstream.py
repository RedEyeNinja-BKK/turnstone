"""Graft: join the upstream stable-line head and the LocalClaw scheduler head.

The reconciled LocalClaw stable-line migration graph carries TWO independent
branches that both descend from the shared upstream/LocalClaw baseline:

    upstream stable-line branch:
        ... -> 070 -> 071 -> 072_memory_index_snapshots   (revision "072")

    LocalClaw scheduler branch:
        ... -> 069 -> lc_run_trigger -> lc_scheduler_claims
             -> merge_071_lclaw (joins upstream 071)
             -> 42e931fce0d1 (re-identified historical LocalClaw no-op "072")

Both are single heads independently.  ``command.upgrade(cfg, "head")`` (the
auto-on-startup migration runner) requires exactly ONE head, so this merge
revision joins the two into a single canonical head.

This is a merge-only revision.  It performs NO schema mutation: the upstream
branch (``072_memory_index_snapshots``) and the LocalClaw scheduler branch
(``lc_run_trigger``/``lc_scheduler_claims``/``merge_071_lclaw``/
``42e931fce0d1``) are schema-additive and orthogonal.

Revision ID: 814990eb0aef
Revises: ("072", "42e931fce0d1")
Create Date: 2026-08-19 (LocalClaw stable-line graft)
"""

from alembic import op  # noqa: F401  (merge-only)

revision = "814990eb0aef"
down_revision = ("072", "42e931fce0d1")  # type: ignore[assignment]
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Merge-only revision — both parent heads already applied their DDL.
    pass


def downgrade() -> None:
    # Merge-only; downgrade across this graft is not a supported rollback path
    # (rollback for this cutover is restore-from-backup, not Alembic downgrade).
    pass
