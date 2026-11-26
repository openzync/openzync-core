"""Drop legacy ``organizations.llm_config`` column (data lives in ``config``).

``config->'llm'`` has been the read path since Alembic revision 0016
backfilled it from the deprecated ``llm_config`` column, and the
repository fallback is removed — the column is write-only dead weight.
Clean break: drop it.

Raw SQL equivalent::

    ALTER TABLE organizations DROP COLUMN llm_config;

Revision ID: 0078
Revises: 0077

Rollback note: downgrade re-adds the column as empty JSONB (server
default ``'{}'``) — per-row LLM data is NOT restored (it already lives
in ``config->'llm'`` since 0016).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the legacy ``llm_config`` column."""
    op.drop_column("organizations", "llm_config")


def downgrade() -> None:
    """Re-add ``llm_config`` as empty JSONB (data not restored)."""
    op.add_column(
        "organizations",
        sa.Column(
            "llm_config",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
