"""Add ``password_hash`` and ``role`` columns to ``users`` for dashboard auth.

Introduces two nullable columns that enable email/password authentication for
dashboard users without affecting existing API-key-only workflows:

- ``password_hash`` — bcrypt hash, ``NULL`` for existing end-users.
- ``role`` — defaults to ``'member'``; ``'admin'`` for org dashboard admins.

A partial unique index on ``email`` (``WHERE email IS NOT NULL``) prevents
duplicate dashboard registrations while allowing multiple ``NULL`` emails
for existing end-user records.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add dashboard auth columns and partial unique index on email."""
    op.add_column(
        "users",
        sa.Column(
            "password_hash",
            sa.Text(),
            nullable=True,
            comment="bcrypt hash — set only for dashboard users (email/password auth).",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "role",
            sa.String(50),
            nullable=False,
            server_default="member",
            comment="User role: 'member' (default) or 'admin' (dashboard).",
        ),
    )
    op.create_index(
        "ix_user_email_unique",
        "users",
        ["email"],
        postgresql_where=sa.text("email IS NOT NULL"),
        unique=True,
    )


def downgrade() -> None:
    """Remove dashboard auth columns and partial unique index."""
    op.drop_index("ix_user_email_unique", table_name="users")
    op.drop_column("users", "role")
    op.drop_column("users", "password_hash")
