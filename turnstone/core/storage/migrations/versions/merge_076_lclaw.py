"""Class-A bootstrap graft: join the LocalClaw scheduler head to upstream 076.

The v1.8.4 line ends at a single head, ``076``. The LocalClaw scheduler branch
(``lc_run_trigger`` -> ``lc_scheduler_claims`` -> ``merge_071_lclaw`` ->
``42e931fce0d1``) is a SEPARATE head that our production database is actually
stamped to (``814990eb0aef`` = the merge of upstream ``072`` and the LocalClaw
branch). Without a joining revision the candidate graph has TWO heads, the
auto-on-startup migration runner refuses to run, and a v1.8.4 runtime cannot
boot against the production database at all:

    ABORT: current DB revision '814990eb0aef' is NOT an ancestor of
           candidate head '076'. Refusing live install.

This revision is merge-only and performs NO schema mutation. Upgrading FROM
``814990eb0aef`` walks the upstream side (073, 074, 075, 076) and then joins,
which is exactly the intended forward migration.

Revision ID: merge_076_lclaw
Revises: ("814990eb0aef", "076")
Create Date: 2026-09-11 (v1.8.4 bootstrap graft)
"""

from alembic import op  # noqa: F401  (merge-only)

revision = "merge_076_lclaw"
down_revision = ("814990eb0aef", "076")  # type: ignore[assignment]
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Merge-only revision - both parent heads already applied their DDL.
    pass


def downgrade() -> None:
    # Merge-only; rollback for this cutover is restore-from-backup, not downgrade.
    pass
