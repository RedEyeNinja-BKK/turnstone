"""Record what each pending MCP OAuth flow was started against.

The consent callback compares the authorization response's ``iss`` with the issuer the user was
sent to (RFC 9207), using whether that authorization server promised to send it, and refuses the
code exchange when the server's identity, URL or audience changed since the flow started. A row
written before this migration carries none of it and reads as expired; such rows live ten minutes.

Revision ID: 085
Revises: 084
Create Date: 2026-10-29
"""

import sqlalchemy as sa
from alembic import op

revision = "085"
down_revision = "084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mcp_oauth_pending") as batch_op:
        batch_op.add_column(sa.Column("as_issuer", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(
            sa.Column("iss_parameter_supported", sa.Integer, nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("server_id", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(sa.Column("server_url", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(sa.Column("audience", sa.Text, nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("mcp_oauth_pending") as batch_op:
        batch_op.drop_column("audience")
        batch_op.drop_column("server_url")
        batch_op.drop_column("server_id")
        batch_op.drop_column("iss_parameter_supported")
        batch_op.drop_column("as_issuer")
