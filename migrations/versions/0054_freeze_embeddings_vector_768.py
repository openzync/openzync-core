"""Freeze embeddings to canonical ``VECTOR(768)`` with HNSW indexes.

Context
-------
Per-org configurable embedding dims forced ``float8[]``/``Text`` storage
plus runtime ``CAST(... AS VECTOR(dim))`` (migration 0017 documents the
abandoned ``vector(4096)`` attempt). Embeddings are now frozen to the
canonical model (``snowflake-arctic-embed-m-v1.5``, 768 dims — see
``core/embeddings.py``), so both columns become native ``VECTOR(768)``
with HNSW cosine indexes.

What this migration does
------------------------
1. ``CREATE EXTENSION IF NOT EXISTS vector``.
2. ``facts`` (``float8[]``): rows whose embedding is non-NULL but not
   exactly 768 dims are reset to ``embedding = NULL, embedded_at = NULL``
   so the existing ``reconcile_enrichment`` → ``embed_fact`` repair pass
   re-embeds them (reuse, no new worker). Retired facts (already NULL)
   are untouched. Remaining rows convert via ``USING embedding::vector``.
3. ``episodes`` (``Text``): a ``DO`` block NULLs every row whose text
   does not cast to ``vector(768)`` (wrong dim, empty string, garbage)
   and clears its ``ENRICHMENT_EMBEDDING`` bit (bit 1, value 2 — see
   ``workers.tasks.base``) so the existing stale-episode pass re-enqueues
   ``embed_episode``. Remaining rows convert via ``USING`` cast.
4. Replaces the 0017 ``CHECK(cardinality(...) > 0)`` constraints:
   ``cardinality()`` does not accept ``vector`` — ``vector_dims() = 768``
   keeps the not-empty semantics and additionally pins the dimension.
5. ``CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)`` on both
   tables.

Rollback note: downgrade drops the HNSW indexes and new CHECKs, converts
the columns back (``facts`` → ``float8[]``, ``episodes`` → ``Text``) and
restores the 0017 cardinality CHECKs. Rows re-embedded as 768-dim vectors
stay 768-dim — downgrade changes storage type only, it does not restore
pre-migration vectors.

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-18 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Convert embedding columns to ``VECTOR(768)`` with HNSW indexes."""
    op.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    # ── facts: NULL wrong-dim rows so reconcile re-embeds them ──────────
    op.execute(
        text("ALTER TABLE facts DROP CONSTRAINT IF EXISTS ck_facts_embedding_not_empty")
    )
    op.execute(
        text(
            "UPDATE facts SET embedding = NULL, embedded_at = NULL "
            "WHERE embedding IS NOT NULL AND cardinality(embedding) != 768"
        )
    )
    op.execute(
        text(
            "ALTER TABLE facts ALTER COLUMN embedding "
            "TYPE vector(768) USING embedding::vector"
        )
    )
    op.create_check_constraint(
        "ck_facts_embedding_canonical_dim",
        "facts",
        text("embedding IS NULL OR vector_dims(embedding) = 768"),
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_facts_embedding_hnsw "
            "ON facts USING hnsw (embedding vector_cosine_ops)"
        )
    )

    # ── episodes: NULL uncastable rows + clear embedding bit ────────────
    # ENRICHMENT_EMBEDDING is bit 1 (value 2) per workers.tasks.base.
    op.execute(
        text(
            "ALTER TABLE episodes "
            "DROP CONSTRAINT IF EXISTS ck_episodes_embedding_not_empty"
        )
    )
    op.execute(
        text(
            """
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN SELECT id, embedding FROM episodes WHERE embedding IS NOT NULL LOOP
        BEGIN
            PERFORM r.embedding::vector(768);
        EXCEPTION WHEN OTHERS THEN
            UPDATE episodes
            SET embedding = NULL,
                enrichment_status = enrichment_status & ~2
            WHERE id = r.id;
        END;
    END LOOP;
END $$;
"""
        )
    )
    op.execute(
        text(
            "ALTER TABLE episodes ALTER COLUMN embedding "
            "TYPE vector(768) USING embedding::vector"
        )
    )
    op.create_check_constraint(
        "ck_episodes_embedding_canonical_dim",
        "episodes",
        text("embedding IS NULL OR vector_dims(embedding) = 768"),
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_episodes_embedding_hnsw "
            "ON episodes USING hnsw (embedding vector_cosine_ops)"
        )
    )


def downgrade() -> None:
    """Drop HNSW indexes + canonical CHECKs, restore pre-0054 storage."""
    op.execute(text("DROP INDEX IF EXISTS ix_episodes_embedding_hnsw"))
    op.execute(text("DROP INDEX IF EXISTS ix_facts_embedding_hnsw"))
    op.execute(
        text(
            "ALTER TABLE episodes "
            "DROP CONSTRAINT IF EXISTS ck_episodes_embedding_canonical_dim"
        )
    )
    op.execute(
        text(
            "ALTER TABLE facts "
            "DROP CONSTRAINT IF EXISTS ck_facts_embedding_canonical_dim"
        )
    )

    op.execute(
        text(
            "ALTER TABLE facts ALTER COLUMN embedding "
            "TYPE float8[] USING embedding::float8[]"
        )
    )
    op.execute(
        text(
            "ALTER TABLE episodes ALTER COLUMN embedding "
            "TYPE text USING embedding::text"
        )
    )

    op.create_check_constraint(
        "ck_facts_embedding_not_empty",
        "facts",
        text("embedding IS NULL OR cardinality(embedding) > 0"),
    )
    op.create_check_constraint(
        "ck_episodes_embedding_not_empty",
        "episodes",
        text("embedding IS NULL OR cardinality(embedding) > 0"),
    )
