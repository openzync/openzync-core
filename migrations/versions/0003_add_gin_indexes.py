"""Add GIN indexes for full-text search on episodes and facts.

Implements BM25 (``to_tsvector``) and fuzzy matching (``pg_trgm``) indexes
required by the Subphase-1c retrieval pipeline.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-06
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create full-text search and trigram indexes.

    Indexes created:
        * ``idx_episodes_fts`` — GIN on ``to_tsvector('english', content)``
          for BM25 ranking on ``episodes``.
        * ``idx_facts_fts`` — GIN on ``to_tsvector('english', content)``
          for BM25 ranking on ``facts``.
        * ``idx_episodes_trgm`` — GIN with ``gin_trgm_ops`` for fuzzy /
          substring matching on ``episodes.content``.
    """
    # GIN index on episodes.content for BM25 full-text search
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_fts ON episodes "
        "USING GIN (to_tsvector('english', content))"
    )
    # GIN index on facts.content for BM25 full-text search
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_facts_fts ON facts "
        "USING GIN (to_tsvector('english', content))"
    )
    # pg_trgm index for fuzzy matching on episodes.content
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_trgm ON episodes "
        "USING GIN (content gin_trgm_ops)"
    )


def downgrade() -> None:
    """Drop all three indexes."""
    op.execute("DROP INDEX IF EXISTS idx_episodes_fts")
    op.execute("DROP INDEX IF EXISTS idx_facts_fts")
    op.execute("DROP INDEX IF EXISTS idx_episodes_trgm")
