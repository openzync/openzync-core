"""Create ingest_dedup table.

Atomic content-dedup claims for batch ingestion — a transaction-scoped
``INSERT ... ON CONFLICT DO NOTHING`` claim that replaces the TOCTOU-prone
Redis check-then-store pattern in the memory service (the Redis key was
written only after the DB transaction committed, so concurrent identical
submissions could both pass).

Revision ID: f1a4503b1f4c
Revises: 0040
Create Date: 2026-08-05
"""

from __future__ import annotations

# Alembic template convention: ``Sequence`` annotates branch_labels /
# depends_on but is never a runtime value.
from collections.abc import Sequence  # noqa: TC003

import sqlalchemy as sa
from alembic import op

revision: str = "f1a4503b1f4c"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``ingest_dedup`` table and its unique content-triple index."""
    op.create_table(
        "ingest_dedup",
        # job_id is the accepted ingest's job UUID — client-supplied, not
        # server-generated (the memory service generates it before the claim).
        sa.Column("job_id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("content_hash", sa.VARCHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        comment="Atomic content-dedup claims for batch ingestion",
    )

    # The unique index IS the dedup arbiter — concurrent identical claims
    # serialize on it inside the caller's transaction.
    op.create_index(
        "uq_ingest_dedup_project_session_hash",
        "ingest_dedup",
        ["project_id", "session_id", "content_hash"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the unique content-triple index and the table."""
    op.drop_index(
        "uq_ingest_dedup_project_session_hash",
        table_name="ingest_dedup",
    )
    op.drop_table("ingest_dedup")
