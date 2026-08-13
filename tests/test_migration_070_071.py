"""Migration 070/071 rehearsals on the exact 1.8.0a6 lineage (head 069).

070 adds ``scheduled_task_runs.trigger`` (historical rows default to
``schedule``); 071 adds the atomic scheduler-lock table and per-task
execution claims (SCHEDULER-CORE CORRECTNESS FIX).  These tests run against
SQLite (fast) and mirror the live PostgreSQL rehearsal.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
import alembic.config
import alembic.command
from pathlib import Path

_MIG_DIR = str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations")


def _sqlite_migrate(db_path: str) -> None:
    cfg = alembic.config.Config()
    cfg.set_main_option("script_location", _MIG_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    alembic.command.upgrade(cfg, "head")


class TestMigration070Trigger:
    def test_upgrade_backfills_historical_rows_to_schedule(self, tmp_path):
        db = tmp_path / "mig.db"
        # Build to 069, seed historical run, then upgrade to head.
        cfg = alembic.config.Config()
        cfg.set_main_option("script_location", _MIG_DIR)
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
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
        assert rev == "071"

    def test_downgrade_drops_trigger(self, tmp_path):
        db = tmp_path / "downgrade.db"
        _sqlite_migrate(str(db))
        cfg = alembic.config.Config()
        cfg.set_main_option("script_location", _MIG_DIR)
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
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


class TestMigration071Claims:
    def test_upgrade_creates_locks_and_claims(self, tmp_path):
        db = tmp_path / "claims.db"
        _sqlite_migrate(str(db))
        eng = sa.create_engine(f"sqlite:///{db}")
        with eng.connect() as c:
            tables = [
                r[0]
                for r in c.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))
                .fetchall()
            ]
            tcols = [
                r[1] for r in c.execute(sa.text("PRAGMA table_info(scheduled_tasks)")).fetchall()
            ]
        assert "scheduler_locks" in tables
        assert "execution_claim_id" in tcols
        assert "execution_claim_until" in tcols
