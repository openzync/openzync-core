"""Add embedded_at timestamp to facts for embedding tracking.

``embed_fact`` previously wrote ``facts.embedding`` directly with no
status tracking, so ``reconcile_enrichment`` could not detect facts whose
embedding never completed or permanently failed.  Facts have exactly one
embedding state, so a single nullable timestamp is sufficient:

* ``embedded_at IS NULL`` + ``embedding IS NULL``  → never attempted (repairable)
* ``embedded_at IS NOT NULL`` + ``embedding IS NULL`` → attempted, retired
  (dimension mismatch — do not re-enqueue)
* ``embedding IS NOT NULL`` → embedded successfully

Revision ID: b7c9d1e2f3a4
Revises: 0040
Create Date: 2026-08-05
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "b7c9d1e2f3a4"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``embedded_at`` timestamptz column to ``facts``."""
    op.add_column(
        "facts",
        sa.Column(
            "embedded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the ``embedded_at`` column."""
    op.drop_column("facts", "embedded_at")
