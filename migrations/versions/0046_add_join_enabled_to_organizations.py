"""Add ``organizations.join_enabled`` — org-code self-registration toggle.

When ``join_enabled`` is FALSE, ``POST /v1/auth/join`` rejects the org
code with 403 (``AuthorizationError``) instead of accepting the member —
admins can pause self-registration without rotating the code or touching
the invite flow.

- Column is NOT NULL with ``server_default=true`` — every existing org
  keeps accepting new members, which is the pre-feature behaviour.
- Pure DDL (``ADD COLUMN`` with a constant default) — no backfill UPDATE,
  so no RLS session statements are needed (unlike 0044, which had to
  bypass RLS for its per-row backfill).

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the NOT NULL ``join_enabled`` column, defaulting to enabled."""
    op.add_column(
        "organizations",
        sa.Column(
            "join_enabled",
            sa.BOOLEAN(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    """Drop the ``join_enabled`` column."""
    op.drop_column("organizations", "join_enabled")
