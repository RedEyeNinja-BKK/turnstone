"""Tests for the avatar storage methods (both backends via the ``db`` fixture)."""

from __future__ import annotations

import threading
import time

import pytest
import sqlalchemy as sa


def _user(db, user_id: str = "u" * 32, username: str = "jin") -> str:
    db.create_user(user_id, username, username.title(), "hash")
    return user_id


class TestUserAvatarStorage:
    def test_no_row_until_something_is_stored(self, db) -> None:
        uid = _user(db)
        assert db.get_user_avatar(uid) is None
        assert db.get_user_avatar_image(uid) is None
        assert db.list_user_avatars() == {}

    def test_writes_need_the_account(self, db) -> None:
        # No write lands before the account exists or after it is deleted.
        assert db.set_user_avatar_color("x" * 32, 2) is False
        assert db.set_user_avatar_image("x" * 32, b"png", "a" * 64) is False
        assert db.get_user_avatar("x" * 32) is None
        uid = _user(db)
        assert db.set_user_avatar_color(uid, 2) is True
        assert db.set_user_avatar_image(uid, b"png", "a" * 64) is True
        assert db.delete_user(uid)
        assert db.set_user_avatar_color(uid, 3) is False
        assert db.get_user_avatar(uid) is None

    def test_a_write_waits_for_a_deletion_in_progress(self, db) -> None:
        # The write waits for the deletion's row lock, then finds no row: it
        # never stores an avatar for an account that is going.
        if db._engine.dialect.name != "postgresql":
            pytest.skip("SQLite runs one writer at a time")
        from turnstone.core.storage._schema import users

        uid = _user(db)
        results: list[bool] = []
        writer = threading.Thread(
            target=lambda: results.append(db.set_user_avatar_color(uid, 2)), name="avatar-write"
        )
        with db._engine.connect() as deleting:
            # What delete_user does, left uncommitted.
            deleting.execute(
                sa.select(users.c.user_id).where(users.c.user_id == uid).with_for_update()
            )
            deleting.execute(sa.delete(users).where(users.c.user_id == uid))
            own_pid = deleting.execute(sa.text("SELECT pg_backend_pid()")).scalar()
            writer.start()
            deadline = time.monotonic() + 10
            while not deleting.execute(
                sa.text(
                    "SELECT count(*) FROM pg_locks"
                    " WHERE NOT granted AND :pid = ANY(pg_blocking_pids(pid))"
                ),
                {"pid": own_pid},
            ).scalar():
                assert time.monotonic() < deadline, "the write never waited"
                time.sleep(0.01)
            deleting.commit()
        writer.join(10)
        assert results == [False]
        assert db.get_user_avatar(uid) is None

    def test_set_color_creates_then_updates_the_row(self, db) -> None:
        uid = _user(db)
        db.set_user_avatar_color(uid, 2)
        assert db.get_user_avatar(uid) == {"color": 2, "image_sha256": None}
        db.set_user_avatar_color(uid, 5)
        assert db.get_user_avatar(uid) == {"color": 5, "image_sha256": None}

    def test_picture_and_colour_are_independent(self, db) -> None:
        uid = _user(db)
        db.set_user_avatar_image(uid, b"png-bytes", "a" * 64)
        assert db.get_user_avatar(uid) == {"color": None, "image_sha256": "a" * 64}
        db.set_user_avatar_color(uid, 1)
        assert db.get_user_avatar_image(uid) == (b"png-bytes", "a" * 64)
        db.set_user_avatar_image(uid, b"other", "b" * 64)
        assert db.get_user_avatar(uid) == {"color": 1, "image_sha256": "b" * 64}
        assert db.get_user_avatar_image(uid) == (b"other", "b" * 64)

    def test_clear_picture_keeps_the_colour(self, db) -> None:
        uid = _user(db)
        db.set_user_avatar_color(uid, 4)
        db.set_user_avatar_image(uid, b"png", "c" * 64)
        # The removed picture's version, read and cleared together.
        assert db.clear_user_avatar_image(uid) == "c" * 64
        assert db.get_user_avatar(uid) == {"color": 4, "image_sha256": None}
        assert db.get_user_avatar_image(uid) is None
        assert db.clear_user_avatar_image(uid) is None

    def test_clear_without_a_picture(self, db) -> None:
        assert db.clear_user_avatar_image(_user(db)) is None
        assert db.clear_user_avatar_image("x" * 32) is None

    def test_list_has_no_picture_bytes(self, db) -> None:
        a = _user(db, "a" * 32, "ana")
        b = _user(db, "b" * 32, "bo")
        db.set_user_avatar_color(a, 0)
        db.set_user_avatar_image(b, b"png", "d" * 64)
        assert db.list_user_avatars() == {
            a: {"color": 0, "image_sha256": None},
            b: {"color": None, "image_sha256": "d" * 64},
        }

    def test_delete_user_deletes_the_avatar(self, db) -> None:
        uid = _user(db)
        db.set_user_avatar_color(uid, 3)
        db.set_user_avatar_image(uid, b"png", "e" * 64)
        assert db.delete_user(uid) is True
        assert db.get_user_avatar(uid) is None
        assert db.list_user_avatars() == {}
