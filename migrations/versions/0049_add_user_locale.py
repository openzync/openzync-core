"""Add per-user locale preference — ``users.locale``.

Backs per-user i18n: transactional email templates are selected by this
BCP-47 tag (``core.locales.ALLOWED_LOCALES``).  ``server_default='en'``
backfills every existing row and keeps the column NOT NULL, so no backfill
UPDATE (and no RLS-bypass statements) is needed — pure DDL.

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the ``locale`` column with an English server default."""
    op.add_column(
        "users",
        sa.Column(
            "locale",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'en'"),
        ),
    )


def downgrade() -> None:
    """Drop the column (existing rows lose their stored preference)."""
    op.drop_column("users", "locale")
