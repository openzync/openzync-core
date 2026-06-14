"""Add summary and summary_updated_at columns to the users table.

Summary captures an auto-generated synopsis of user conversations,
updated periodically by a background worker.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-14
"""

from __future__ import annotations

from typing import ClassVar

from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "summary_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "summary_updated_at")
    op.drop_column("users", "summary")
