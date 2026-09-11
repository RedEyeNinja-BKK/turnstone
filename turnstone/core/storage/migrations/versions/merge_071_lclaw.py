"""Join the upstream 1.8.0a7 lineage with the LocalClaw scheduler lineage.

Upstream post-1.8.0a6 owns 070_model_max_concurrency (revision 070) and
071_conversations_commit_key (revision 071).  The LocalClaw Run Once /
scheduler-core branch forks from 069 and owns lc_run_trigger and
lc_scheduler_claims.  Both are single heads independently; the canonical
fork must expose exactly ONE head so the auto-on-startup migration runner
(``command.upgrade(cfg, "head")``) never fails with "Multiple head
revisions".

This is a merge-only revision.  It performs NO schema mutation: the two
lineages are schema-additive and orthogonal (upstream adds
model_definitions.max_concurrency + conversations.commit_key; LocalClaw
adds scheduled_task_runs.trigger + scheduler_locks +
scheduled_tasks.execution_claim_*).  The merge exists purely to reconcile
graph ancestry.

Revision ID: merge_071_lclaw
Revises: ("071", "lc_scheduler_claims")
Create Date: 2026-08-14
"""

revision = "merge_071_lclaw"
down_revision = ("071", "lc_scheduler_claims")  # type: ignore[assignment]
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Merge-only revision — both parent lineages already applied their DDL.
    pass


def downgrade() -> None:
    # No schema change to reverse.  Downgrade past this merge is not a
    # supported cross-branch operation; the rollback boundary is documented
    # in the Run-Once canonicalization gate evidence.
    pass
