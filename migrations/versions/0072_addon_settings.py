"""Add addon_states.settings JSONB (Phase 0 generic per-addon settings).

Revision ID: 0072
Revises: 0071
Create Date: 2026-11-23

NOT NULL with ``server_default '{}'`` so existing rows backfill to the
empty object; rollback drops the column (stored values are not
recoverable — settings re-seed from manifest defaults on next write).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ``settings`` JSONB column to ``addon_states``."""
    op.add_column(
        "addon_states",
        sa.Column(
            "settings",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    """Drop the ``settings`` column (stored values are not recoverable)."""
    op.drop_column("addon_states", "settings")
