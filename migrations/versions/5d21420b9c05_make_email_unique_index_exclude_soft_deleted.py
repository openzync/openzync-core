"""make email unique index exclude soft-deleted users

Revision ID: 5d21420b9c05
Revises: 2089cbf56394
Create Date: 2026-06-11 14:15:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5d21420b9c05"
down_revision: Union[str, None] = "2089cbf56394"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old unique index (which only excluded NULL emails).
    op.drop_index("ix_user_email_unique", table_name="users")
    # Create a new partial unique index that also excludes soft-deleted rows.
    # This allows reusing an email after a user is soft-deleted.
    op.create_index(
        "ix_user_email_unique",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL AND is_deleted = false"),
    )


def downgrade() -> None:
    op.drop_index("ix_user_email_unique", table_name="users")
    op.create_index(
        "ix_user_email_unique",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
