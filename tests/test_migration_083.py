"""Tests for alembic migration 083 (avatar columns on users, colours for existing users).

Drives ``command.upgrade``/``downgrade`` against an isolated SQLite database per
test (the 060/062/063/065/066 harness pattern), then asserts:

* upgrade adds the avatar columns to ``users`` and gives every existing user a colour, picked
  oldest first by the rule ``turnstone.core.avatars`` applied to new accounts
  when 083 was written;
* the migration's written-out copy of that rule keeps giving the answers pinned
  here, which are literal so that a later change to the live module never asks
  for an edit to this migration;
* downgrade drops the columns, and up -> down -> up lands cleanly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from turnstone.core import avatars

if TYPE_CHECKING:
    from types import ModuleType

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)
_MIGRATION_FILE = Path(_MIGRATIONS_DIR) / "versions" / "083_user_avatars.py"


def _alembic_cfg(db: Path | str) -> Config:
    """Config for a SQLite file path, or for a database URL string."""
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    url = db if isinstance(db, str) else f"sqlite:///{db}"
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_083", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _s(*code_points: int) -> str:
    return "".join(chr(cp) for cp in code_points)


_USERS = [
    # (user_id, username, display_name, created): three J's, oldest first.
    ("0123456789abcdef0123456789abcdef", "jin", "Jin Park", "2026-01-01T00:00:00"),
    ("5f3b2a90c4d14e6f8a7b9c0d1e2f3a4b", "jo", "Jo Chen", "2026-01-02T00:00:00"),
    ("ffffffffffffffffffffffffffffffff", "jules", "", "2026-01-03T00:00:00"),
    ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "ana", "Ana Ruiz", "2026-01-04T00:00:00"),
]


_AVATAR_COLUMNS = {"avatar_color", "avatar_image", "avatar_sha256"}


def _user_columns(engine: sa.Engine) -> set[str]:
    return {column["name"] for column in sa.inspect(engine).get_columns("users")}


def _insert_users(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        for user_id, username, display_name, created in _USERS:
            conn.execute(
                sa.text(
                    "INSERT INTO users (user_id, username, display_name, password_hash, created) "
                    "VALUES (:u, :n, :d, 'x', :c)"
                ),
                {"u": user_id, "n": username, "d": display_name, "c": created},
            )


def _assert_backfill(url: str) -> None:
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "082")
    engine = sa.create_engine(url)
    try:
        _insert_users(engine)
        command.upgrade(cfg, "083")
        with engine.connect() as conn:
            rows = dict(
                conn.execute(
                    sa.text(
                        "SELECT user_id, avatar_color FROM users WHERE avatar_image IS NULL "
                        "AND avatar_sha256 IS NULL"
                    )
                ).fetchall()
            )
    finally:
        engine.dispose()

    assert rows == {
        "0123456789abcdef0123456789abcdef": 3,
        "5f3b2a90c4d14e6f8a7b9c0d1e2f3a4b": 5,
        "ffffffffffffffffffffffffffffffff": 0,
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": 0,
    }
    # The three J's all differ.
    assert len({rows[u[0]] for u in _USERS[:3]}) == 3


class TestMigration083:
    def test_existing_users_get_colours_picked_oldest_first(self, tmp_path: Path) -> None:
        _assert_backfill(f"sqlite:///{tmp_path / '083-up.db'}")

    def test_existing_users_get_colours_on_postgresql(self, fresh_pg_url: Any) -> None:
        _assert_backfill(fresh_pg_url.render_as_string(hide_password=False))

    def test_empty_users_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "083-empty.db"
        command.upgrade(_alembic_cfg(db_path), "083")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            assert _user_columns(engine) >= _AVATAR_COLUMNS
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        sa.text("SELECT COUNT(*) FROM users WHERE avatar_color IS NOT NULL")
                    ).scalar()
                    == 0
                )
        finally:
            engine.dispose()

    def test_written_out_rule_is_pinned(self) -> None:
        m = _migration()
        assert m._PALETTE_SIZE == 6
        assert {
            frozenset(pair)
            for pair in [
                (0, 1),
                (0, 5),
                (1, 2),
                (1, 3),
                (1, 5),
                (2, 3),
                (2, 4),
                (2, 5),
                (3, 4),
                (4, 5),
            ]
        } == m._CONFUSABLE
        initials = [
            (("Jin Park", ""), "J"),
            (("", "jin"), "J"),
            ((_s(0x738B, 0x5C0F), ""), _s(0x738B)),
            ((_s(0x915, 0x94D, 0x937), ""), _s(0x915)),
            ((_s(0x1F98A) + " Fox", ""), "F"),
            ((_s(0xDF) + "tefan", ""), _s(0xDF)),
            ((_s(0xFF21, 0xFF4E), ""), "A"),
            (("ilker", ""), "I"),
            (("___", ""), ""),
            # Titlecase keeps a Georgian letter in its ordinary form.
            ((_s(0x10D2, 0x10D8, 0x10DD), ""), _s(0x10D2)),
            # A Thai vowel written before its consonant, and the okina, are skipped.
            ((_s(0x0E40, 0x0E08, 0x0E19), ""), _s(0x0E08)),
            ((_s(0x02BB) + "Iolani", ""), "I"),
        ]
        for (display_name, username), letter in initials:
            assert m._initial(display_name, username) == letter, display_name
        # The hashed colour is also the live fallback for a missing colour, so
        # the two must agree for good.
        ids = [f"{n:032x}" for n in range(0, 2000, 37)] + ["channel-gateway"]
        for user_id in ids:
            assert m._hashed_color(user_id) == avatars.hashed_color(user_id)
        per_color = [
            [0] * 6,
            [1, 0, 0, 0, 0, 0],
            [1, 1, 1, 1, 1, 0],
            [2, 1, 2, 2, 2, 2],
            [0, 3, 0, 0, 0, 0],
        ]
        picks = {
            "0123456789abcdef0123456789abcdef": [3, 3, 5, 1, 4],
            "channel-gateway": [4, 4, 5, 1, 4],
            "ffffffffffffffffffffffffffffffff": [0, 2, 5, 1, 4],
        }
        for user_id, expected in picks.items():
            assert [m._pick_color(user_id, per) for per in per_color] == expected, user_id

    def test_downgrade_then_upgrade(self, tmp_path: Path) -> None:
        db_path = tmp_path / "083-down.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "083")
        command.downgrade(cfg, "082")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            assert not _AVATAR_COLUMNS & _user_columns(engine)
        finally:
            engine.dispose()
        command.upgrade(cfg, "083")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            assert _user_columns(engine) >= _AVATAR_COLUMNS
        finally:
            engine.dispose()
