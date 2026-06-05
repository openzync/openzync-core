"""Add is_deleted column to users table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_index("idx_users_active", "users", ["organization_id"], postgresql_where=sa.text("is_deleted = false"))


def downgrade() -> None:
    op.drop_index("idx_users_active", table_name="users")
    op.drop_column("users", "is_deleted")
