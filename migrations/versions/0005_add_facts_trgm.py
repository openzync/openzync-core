"""Add pg_trgm GIN index on facts.content for fuzzy full-text search.

Implements the ``idx_facts_trgm`` index (GIN with ``gin_trgm_ops``) required
by the Subphase-2a retrieval pipeline for fuzzy / substring matching on facts.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-06
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create pg_trgm GIN index on facts.content.

    This index enables fuzzy / substring matching via the ``similarity()``
    and ``%`` operators, complementing the existing BM25 GIN index
    (``idx_facts_fts`` created in migration 0003).

    The ``pg_trgm`` extension must be installed (added in migration 0003).
    """
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_facts_trgm ON facts "
        "USING GIN (content gin_trgm_ops)"
    )


def downgrade() -> None:
    """Drop the pg_trgm index on facts.content."""
    op.execute("DROP INDEX IF EXISTS idx_facts_trgm")
