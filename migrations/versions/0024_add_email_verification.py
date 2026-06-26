"""Add email verification columns to users table.

Adds columns for tracking email verification status and storing
verification tokens:
- ``email_verified`` — boolean, default false
- ``email_verified_at`` — nullable timestamp
- ``verification_token_hash`` — nullable SHA-256 hash (64 chars)
- ``verification_token_expires_at`` — nullable timestamp

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-26
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="Whether the user has verified their email address.",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "email_verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp of email verification.",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "verification_token_hash",
            sa.String(64),
            nullable=True,
            comment="SHA-256 hash of the email verification token.",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "verification_token_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Expiration timestamp for the verification token.",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "verification_token_expires_at")
    op.drop_column("users", "verification_token_hash")
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email_verified")
