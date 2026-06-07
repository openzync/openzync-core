"""Add ``episode_id`` to ``structured_extractions`` for per-episode granularity.

Previously the table was session-scoped only.  Adding ``episode_id`` enables:

* Per-episode traceability (which conversation segment produced which data).
* Consistent enrichment pattern matching all other workers.
* ``(episode_id, schema_id)`` unique constraint for idempotent re-processing.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Pre-migration: clear any existing rows ──────────────────────────────
    # This is safe because we are in early development — there are no production
    # rows in this table.  We delete rather than add a nullable column because
    # every extraction row *must* have an episode_id going forward.
    op.execute("DELETE FROM structured_extractions")

    # ── Add episode_id column ───────────────────────────────────────────────
    op.add_column(
        "structured_extractions",
        sa.Column("episode_id", sa.Uuid(), nullable=False),
    )

    # ── Foreign key ─────────────────────────────────────────────────────────
    op.create_foreign_key(
        "fk_structured_extractions_episode_id",
        "structured_extractions",
        "episodes",
        ["episode_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # ── Indexes ─────────────────────────────────────────────────────────────
    op.create_index(
        "ix_structured_extractions_episode_id",
        "structured_extractions",
        ["episode_id"],
    )
    op.create_index(
        "ix_structured_extractions_session_id",
        "structured_extractions",
        ["session_id"],
    )

    # ── Unique constraint for idempotency ───────────────────────────────────
    op.create_unique_constraint(
        "uq_structured_extraction_episode_schema",
        "structured_extractions",
        ["episode_id", "schema_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_structured_extraction_episode_schema",
        "structured_extractions",
    )
    op.drop_index("ix_structured_extractions_session_id")
    op.drop_index("ix_structured_extractions_episode_id")
    op.drop_constraint(
        "fk_structured_extractions_episode_id",
        "structured_extractions",
        type_="foreignkey",
    )
    op.drop_column("structured_extractions", "episode_id")
