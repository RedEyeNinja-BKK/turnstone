"""Add avatar columns to users and give every existing user a colour.

Issue 997: ``avatar_color`` is the palette index of the generated avatar (one
initial on one of six colours, drawn by the page), ``avatar_image`` an uploaded
picture already normalised to a 256px PNG, ``avatar_sha256`` its version.  They
live on the users row, so the avatar goes with the user.  New accounts get a
colour picked at creation so that people who share an initial differ
(``turnstone.core.avatars.assign_avatar_color``).  This migration applies the
same pick to the users that already exist, oldest first, as if each had been
created after the feature shipped.

The pick is written out here rather than imported, so a later change to
``turnstone.core.avatars`` does not rewrite this migration; a test pins this
copy's answers.

Revision ID: 083
Revises: 082
Create Date: 2026-10-13
"""

import hashlib
import unicodedata

import sqlalchemy as sa
from alembic import op

revision = "083"
down_revision = "082"
branch_labels = None
depends_on = None


_PALETTE_SIZE = 6

# Palette pairs a viewer with protanopia, deuteranopia or tritanopia can't
# tell apart (orange, green, teal, blue, violet, pink = 0-5).
_CONFUSABLE = frozenset(
    frozenset(pair)
    for pair in (
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
    )
)


# Leading vowels (Thai, Lao, New Tai Lue, Tai Viet) and spacing modifier
# letters, which never stand for a name.
_NOT_INITIALS = frozenset(
    (
        *range(0x0E40, 0x0E45),
        *range(0x0EC0, 0x0EC5),
        *range(0x19B5, 0x19B8),
        0x19BA,
        0xAAB5,
        0xAAB6,
        0xAAB9,
        0xAABB,
        0xAABC,
        *range(0x02B0, 0x0300),
    )
)


def _first_letter(text: str) -> str:
    for ch in unicodedata.normalize("NFKC", text or ""):
        if unicodedata.category(ch)[0] in ("L", "N") and ord(ch) not in _NOT_INITIALS:
            title = ch.title()
            return title if len(title) == 1 else ch
    return ""


def _initial(display_name: str, username: str) -> str:
    return _first_letter(display_name) or _first_letter(username)


def _hashed_color(user_id: str) -> int:
    digest = hashlib.sha256(user_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % _PALETTE_SIZE


def _pick_color(user_id: str, per_color: list[int]) -> int:
    """The colour for one user, given how many people with their initial have each."""
    hashed = _hashed_color(user_id)

    def cost(color: int) -> tuple[int, int, int]:
        return (
            per_color[color],
            sum(
                count
                for other, count in enumerate(per_color)
                if frozenset((other, color)) in _CONFUSABLE
            ),
            (color - hashed) % _PALETTE_SIZE,
        )

    return min(range(_PALETTE_SIZE), key=cost)


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_color", sa.Integer, nullable=True))
    op.add_column("users", sa.Column("avatar_image", sa.LargeBinary, nullable=True))
    op.add_column("users", sa.Column("avatar_sha256", sa.Text, nullable=True))

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT user_id, username, display_name FROM users ORDER BY created, user_id")
    ).fetchall()
    if not rows:
        return
    # Per initial, how many of the users picked so far have each colour.
    counts: dict[str, list[int]] = {}
    picks = []
    for user_id, username, display_name in rows:
        per_color = counts.setdefault(
            _initial(display_name or "", username or ""), [0] * _PALETTE_SIZE
        )
        color = _pick_color(user_id, per_color)
        per_color[color] += 1
        picks.append({"user_id": user_id, "color": color})
    bind.execute(sa.text("UPDATE users SET avatar_color = :color WHERE user_id = :user_id"), picks)


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("avatar_sha256")
        batch_op.drop_column("avatar_image")
        batch_op.drop_column("avatar_color")
