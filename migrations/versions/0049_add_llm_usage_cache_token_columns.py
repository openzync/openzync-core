"""Add cache token columns to ``llm_usage`` — provider-side prompt caching.

Anthropic reports ``cache_read_input_tokens`` (tokens served from the
provider cache) and ``cache_creation_input_tokens`` (tokens written to the
cache) separately from ``prompt_tokens``.  Both columns are NOT NULL with
``server_default 0`` so existing rows and inserts that don't set them
remain valid.  Pure DDL — no backfill needed.

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the two cache token columns to ``llm_usage``."""
    op.add_column(
        "llm_usage",
        sa.Column(
            "cache_read_input_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "llm_usage",
        sa.Column(
            "cache_creation_input_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    """Drop the two cache token columns.

    Rollback note: run ``alembic downgrade -1`` with the same
    ``OZ_DATABASE_URL`` used for the upgrade.  Any cache token values
    written since the upgrade are lost with the columns.
    """
    op.drop_column("llm_usage", "cache_creation_input_tokens")
    op.drop_column("llm_usage", "cache_read_input_tokens")
