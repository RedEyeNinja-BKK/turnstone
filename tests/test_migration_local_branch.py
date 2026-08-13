"""LocalClaw branch migration rehearsals on the canonicalized fork lineage.

The canonical fork joins the upstream 1.8.0a7 lineage (069 -> 070 -> 071)
with the LocalClaw scheduler lineage (069 -> lc_run_trigger ->
lc_scheduler_claims) via the merge-only revision ``merge_071_lclaw``.

- lc_run_trigger adds ``scheduled_task_runs.trigger`` (historical rows
  default to ``schedule``).
- lc_scheduler_claims adds the atomic scheduler-lock table and per-task
  execution claims (SCHEDULER-CORE CORRECTNESS FIX).
- upstream 070 adds ``model_definitions.max_concurrency``; upstream 071 adds
  ``conversations.commit_key``.
- merge_071_lclaw reconciles the two heads into ONE canonical head.

These tests run against SQLite (fast) and mirror the live PostgreSQL
rehearsal from the Run-Once migration-lineage gate.
"""

from __future__ import annotations

from pathlib import Path

import alembic.command
import alembic.config
import sqlalchemy as sa

_MIG_DIR = str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations")
_HEAD = "merge_071_lclaw"


def _cfg(db_path) -> alembic.config.Config:
    cfg = alembic.config.Config()
    cfg.set_main_option("script_location", _MIG_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _sqlite_migrate(db_path: str) -> None:
    alembic.command.upgrade(_cfg(db_path), "head")


class TestCanonicalHead:
    def test_fresh_db_upgrades_to_merged_head(self, tmp_path):
        db = tmp_path / "fresh.db"
        _sqlite_migrate(str(db))
        eng = sa.create_engine(f"sqlite:///{db}")
        with eng.connect() as c:
            rev = c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            tables = [
                r[0]
                for r in c.execute(
                    sa.text("SELECT name FROM sqlite_master WHERE type='table'")
                ).fetchall()
            ]
            st_cols = [
                r[1] for r in c.execute(sa.text("PRAGMA table_info(scheduled_tasks)")).fetchall()
            ]
            runs_cols = [
                r[1]
                for r in c.execute(sa.text("PRAGMA table_info(scheduled_task_runs)")).fetchall()
            ]
            md_cols = [
                r[1] for r in c.execute(sa.text("PRAGMA table_info(model_definitions)")).fetchall()
            ]
            conv_cols = [
                r[1] for r in c.execute(sa.text("PRAGMA table_info(conversations)")).fetchall()
            ]
        assert rev == _HEAD
        assert "scheduler_locks" in tables
        assert "execution_claim_id" in st_cols
        assert "execution_claim_until" in st_cols
        assert "trigger" in runs_cols
        assert "max_concurrency" in md_cols  # upstream 070 applied
        assert "commit_key" in conv_cols  # upstream 071 applied


class TestMigrationLcRunTrigger:
    def test_upgrade_backfills_historical_rows_to_schedule(self, tmp_path):
        db = tmp_path / "mig.db"
        # Build to 069, seed historical run, then upgrade to merged head.
        cfg = _cfg(db)
        alembic.command.upgrade(cfg, "069")
        eng = sa.create_engine(f"sqlite:///{db}")
        with eng.begin() as c:
            c.execute(
                sa.text(
                    "INSERT INTO scheduled_task_runs (run_id, task_id, node_id, ws_id,"
                    " correlation_id, started, status, error) VALUES"
                    " ('r_old', 't1', 'n1', '', '', '2020-01-01T00:00:00', 'dispatched', '')"
                )
            )
        alembic.command.upgrade(cfg, "head")
        with eng.connect() as c:
            trig = c.execute(
                sa.text("SELECT trigger FROM scheduled_task_runs WHERE run_id='r_old'")
            ).scalar()
            rev = c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        assert trig == "schedule"
        assert rev == _HEAD

    def test_downgrade_drops_trigger(self, tmp_path):
        db = tmp_path / "downgrade.db"
        _sqlite_migrate(str(db))
        cfg = _cfg(db)
        alembic.command.downgrade(cfg, "069")
        eng = sa.create_engine(f"sqlite:///{db}")
        with eng.connect() as c:
            cols = [
                r[1]
                for r in c.execute(sa.text("PRAGMA table_info(scheduled_task_runs)")).fetchall()
            ]
            rev = c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        assert "trigger" not in cols
        assert rev == "069"


class TestMigrationLcSchedulerClaims:
    def test_upgrade_creates_locks_and_claims(self, tmp_path):
        db = tmp_path / "claims.db"
        _sqlite_migrate(str(db))
        eng = sa.create_engine(f"sqlite:///{db}")
        with eng.connect() as c:
            tables = [
                r[0]
                for r in c.execute(
                    sa.text("SELECT name FROM sqlite_master WHERE type='table'")
                ).fetchall()
            ]
            tcols = [
                r[1] for r in c.execute(sa.text("PRAGMA table_info(scheduled_tasks)")).fetchall()
            ]
        assert "scheduler_locks" in tables
        assert "execution_claim_id" in tcols
        assert "execution_claim_until" in tcols


class TestProductionPathMerge:
    def test_production_local_branch_merges_preserving_data(self, tmp_path):
        """Production path: 069 -> lc_run_trigger -> lc_scheduler_claims (with
        data) -> merge_071_lclaw.  Data must survive and upstream features must
        be present."""
        db = tmp_path / "prod.db"
        cfg = _cfg(db)
        alembic.command.upgrade(cfg, "069")
        eng = sa.create_engine(f"sqlite:///{db}")
        with eng.begin() as c:
            c.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks (task_id, name, schedule_type, cron_expr,"
                    " enabled, created_by, initial_message, created, updated)"
                    " VALUES ('t_prod','prod schedule','cron','0 0 * * *',1,'u','msg',"
                    " '2020-01-01T00:00:00','2020-01-01T00:00:00')"
                )
            )
            c.execute(
                sa.text(
                    "INSERT INTO scheduled_task_runs (run_id, task_id, node_id, ws_id,"
                    " correlation_id, started, status, error) VALUES"
                    " ('r_hist','t_prod','n1','','','2020-01-01T00:00:00','dispatched','')"
                )
            )
        alembic.command.upgrade(cfg, "lc_run_trigger")
        alembic.command.upgrade(cfg, "lc_scheduler_claims")
        with eng.begin() as c:
            c.execute(
                sa.text(
                    "INSERT INTO scheduled_task_runs (run_id, task_id, node_id, ws_id,"
                    " correlation_id, started, status, error, trigger) VALUES"
                    " ('r_manual','t_prod','n1','','','2020-01-02T00:00:00','dispatched','','manual')"
                )
            )
            c.execute(
                sa.text(
                    "INSERT INTO scheduler_locks (lock_name, owner, until)"
                    " VALUES ('scheduler','console-1','2099-01-01T00:00:00')"
                )
            )
            c.execute(
                sa.text(
                    "UPDATE scheduled_tasks SET execution_claim_id='c1',"
                    " execution_claim_until='2099-01-01T00:00:00' WHERE task_id='t_prod'"
                )
            )
        alembic.command.upgrade(cfg, "head")
        with eng.connect() as c:
            rev = c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            runs = c.execute(
                sa.text("SELECT run_id, trigger FROM scheduled_task_runs ORDER BY started")
            ).fetchall()
            lock = c.execute(sa.text("SELECT lock_name, owner FROM scheduler_locks")).fetchall()
            claim = c.execute(
                sa.text("SELECT execution_claim_id FROM scheduled_tasks WHERE task_id='t_prod'")
            ).scalar()
            md_cols = [
                r[1] for r in c.execute(sa.text("PRAGMA table_info(model_definitions)")).fetchall()
            ]
            conv_cols = [
                r[1] for r in c.execute(sa.text("PRAGMA table_info(conversations)")).fetchall()
            ]
        assert rev == _HEAD
        assert runs == [("r_hist", "schedule"), ("r_manual", "manual")]
        assert lock == [("scheduler", "console-1")]
        assert claim == "c1"
        assert "max_concurrency" in md_cols
        assert "commit_key" in conv_cols
