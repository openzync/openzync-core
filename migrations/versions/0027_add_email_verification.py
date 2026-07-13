"""Add email verification columns to users table.

Adds ``is_email_verified`` and ``email_verified_at`` to support the email
verification flow introduced in Phase 2.  Existing verified users (those
with a ``password_hash`` set) are backfilled with ``is_email_verified=true``.

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Add columns ─────────────────────────────────────────────────────────
    op.add_column(
        "users",
        sa.Column(
            "is_email_verified",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "email_verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # ── Backfill existing users who have a password hash ──────────────────
    # (they are already verified — they existed before email verification
    #  was introduced).
    op.execute(
        "UPDATE users SET is_email_verified = true "
        "WHERE password_hash IS NOT NULL AND is_deleted = false"
    )


def downgrade() -> None:
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "is_email_verified")
