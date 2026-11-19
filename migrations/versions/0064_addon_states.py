"""Create addon_states table.

Revision ID: 0064
Revises: 0063
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``addon_states`` platform-global registry table."""
    op.create_table(
        "addon_states",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("installed_sha", sa.Text(), nullable=True),
        sa.Column("latest_seen", sa.Text(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "update_available",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "uid",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        comment="Platform-global addon install registry (platform-wide enablement)",
    )


def downgrade() -> None:
    """Drop the ``addon_states`` table."""
    op.drop_table("addon_states")
