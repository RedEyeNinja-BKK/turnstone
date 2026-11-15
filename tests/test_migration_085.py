"""Migration 085: a pending MCP OAuth flow records what it was started against (#1408).

A flow an older process started reads as unrecorded (an empty issuer), which the consent callback
treats as expired.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_NEW_COLUMNS = {"as_issuer", "iss_parameter_supported", "server_id", "server_url", "audience"}


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _columns(url: str) -> set[str]:
    engine = sa.create_engine(url)
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns("mcp_oauth_pending")}
    finally:
        engine.dispose()


def _upgrade_with_a_flow_in_progress(url: str) -> tuple[Any, ...]:
    """084, a pending row as an older process wrote it, 085; return its new columns."""
    cfg = _config(url)
    command.upgrade(cfg, "084")
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO mcp_oauth_pending "
                    "(state, user_id, server_name, code_verifier, return_url, created_at) "
                    "VALUES ('s1', 'u1', 'srv', 'v', '/', '2026-10-29T00:00:00')"
                )
            )
        command.upgrade(cfg, "085")
        with engine.connect() as conn:
            return tuple(
                conn.execute(
                    sa.text(
                        "SELECT as_issuer, iss_parameter_supported, server_id, server_url, "
                        "audience FROM mcp_oauth_pending WHERE state = 's1'"
                    )
                ).one()
            )
    finally:
        engine.dispose()


def test_a_flow_started_before_the_upgrade_reads_as_unrecorded(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / '085-up.db'}"
    assert _upgrade_with_a_flow_in_progress(url) == ("", 0, "", "", "")


def test_downgrade_then_upgrade_round_trip(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / '085-roundtrip.db'}"
    cfg = _config(url)
    command.upgrade(cfg, "085")
    command.downgrade(cfg, "084")
    downgraded = _columns(url)
    command.upgrade(cfg, "085")
    upgraded = _columns(url)

    assert not downgraded & _NEW_COLUMNS
    assert upgraded >= _NEW_COLUMNS


def test_a_flow_started_before_the_upgrade_reads_as_unrecorded_on_postgresql(
    fresh_pg_url: Any,
) -> None:
    url = fresh_pg_url.render_as_string(hide_password=False)
    assert _upgrade_with_a_flow_in_progress(url) == ("", 0, "", "", "")
