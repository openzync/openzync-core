"""Add is_deleted column to users table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_index("idx_users_active", "users", ["organization_id"], postgresql_where=sa.text("is_deleted = false"))


def downgrade() -> None:
    op.drop_index("idx_users_active", table_name="users")
    op.drop_column("users", "is_deleted")
