"""Add graph_observations table for deferred graph-topology observations.

The ``graph_observations`` table stores structured observation records
surfaced by a second inference pass over graph topology (not over raw
text).  Examples of observations:

- *Entity A and Entity B always appear together in support tickets.*
- *User asks about pricing within 2 weeks of every product launch.*
- *Entity X consistently churns after Entity Y downgrades.*

Each observation has a type (``co_occurrence``, ``temporal_pattern``,
``behavioral_pattern``), an optional related entity (for pair-level
observations), and lists of supporting fact / relationship IDs.

The table uses a **functional unique index** with a sentinel UUID to
handle the nullable ``related_entity_id`` column — PostgreSQL treats
NULL != NULL in unique indexes, so entity-level observations (where
``related_entity_id`` is NULL) would otherwise have no dedup.

``ENRICHMENT_OBSERVATIONS`` (bit 6) is already reserved in
``workers/tasks/base.py``.  The worker that populates this table
(``compute_observations``) runs as a low-priority ARQ task triggered
after ``link_entities_to_episode`` (bit 3) completes.

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ════════════════════════════════════════════════════════════════════════════
    # STEP 1: Create the graph_observations table
    # ════════════════════════════════════════════════════════════════════════════
    # Column design:
    #   - subject_entity_id: the entity this observation is about (NOT NULL).
    #   - related_entity_id: the other entity in a pair observation (e.g.
    #     co-occurrence partner); NULL for entity-level observations
    #     (temporal patterns, behavioral patterns).
    #   - observation_type: e.g. co_occurrence, temporal_pattern,
    #     behavioral_pattern.  Extensible via ObservationType StrEnum.
    #   - content: natural-language description (LLM-generated, or
    #     template fallback).
    #   - supporting_fact_ids / supporting_relationship_ids: evidence
    #     that supports this observation.
    #   - confidence: how confident the system is in this observation.
    #   - valid_from / valid_to: temporal validity of the observation
    #     itself (distinct from the temporal range of the supporting facts).
    #   - metadata: JSONB escape hatch for future extensibility.
    op.create_table(
        "graph_observations",
        sa.Column("id", sa.UUID(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("subject_entity_id", sa.UUID(), nullable=False),
        sa.Column("related_entity_id", sa.UUID(), nullable=True,
                  comment="For pair-level observations (e.g. co-occurrence): "
                          "the other entity in the pair. NULL for entity-level "
                          "observations (temporal patterns, behavioral patterns)."),
        sa.Column("observation_type", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("supporting_fact_ids",
                  postgresql.ARRAY(sa.UUID()), nullable=True),
        sa.Column("supporting_relationship_ids",
                  postgresql.ARRAY(sa.UUID()), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False,
                  server_default="0.0"),
        sa.Column("valid_from", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("valid_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("observation_metadata", postgresql.JSONB(), nullable=True,
                  comment="Arbitrary metadata for future extensibility."),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subject_entity_id"], ["graph_entities.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["related_entity_id"], ["graph_entities.id"],
            ondelete="SET NULL",
        ),
    )

    # ════════════════════════════════════════════════════════════════════════════
    # STEP 2: Indexes
    # ════════════════════════════════════════════════════════════════════════════

    # Tenant + project scoping
    op.create_index("idx_observations_project", "graph_observations",
                    ["project_id"])
    op.create_index("idx_observations_org", "graph_observations",
                    ["organization_id"])

    # "What observations exist about this entity?"
    op.create_index("idx_observations_subject", "graph_observations",
                    ["subject_entity_id"])

    # Filter by type within a project
    op.create_index("idx_observations_type", "graph_observations",
                    ["observation_type"])

    # Composite for common query: all co_occurrence observations in a project
    op.create_index("idx_observations_project_type", "graph_observations",
                    ["project_id", "observation_type"])

    # Fast lookups of pair-level observations
    op.create_index("idx_observations_pair", "graph_observations",
                    ["subject_entity_id", "related_entity_id"],
                    postgresql_where=sa.text("related_entity_id IS NOT NULL"))

    # ── Functional unique index for dedup ─────────────────────────────────────
    # PostgreSQL's unique B-tree index treats NULL != NULL, so a plain unique
    # on (project_id, subject_entity_id, observation_type, related_entity_id)
    # would allow unlimited entity-level observations (where related_entity_id
    # IS NULL) for the same entity + type.
    #
    # Solution: COALESCE NULL to a sentinel all-zeros UUID.  Entity-level obs
    # all get the sentinel in the index, so duplicates collide.  Pair-level obs
    # get the actual UUID.  The sentinel never appears as a real column value
    # because related_entity_id IS NULL for entity-level observations, and the
    # FK to graph_entities.id prevents 0000... from being stored as a real id.
    op.create_index(
        "idx_observations_dedup",
        "graph_observations",
        [
            "project_id",
            "subject_entity_id",
            "observation_type",
            sa.text("COALESCE(related_entity_id, "
                    "'00000000-0000-0000-0000-000000000000'::uuid)"),
        ],
        unique=True,
    )

    # ════════════════════════════════════════════════════════════════════════════
    # STEP 3: Row-level security (org isolation)
    # ════════════════════════════════════════════════════════════════════════════
    op.execute("ALTER TABLE graph_observations ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation_graph_observations ON graph_observations
        FOR ALL
        USING (
            current_setting('app.bypass_rls', true) = 'true'
            OR organization_id = current_setting('app.org_id')::UUID
        )
    """)


def downgrade() -> None:
    # ── 1. Drop RLS policy ────────────────────────────────────────────────────
    op.execute(
        "DROP POLICY IF EXISTS org_isolation_graph_observations "
        "ON graph_observations"
    )

    # ── 2. Drop indexes ───────────────────────────────────────────────────────
    op.drop_index("idx_observations_dedup", table_name="graph_observations")
    op.drop_index("idx_observations_pair", table_name="graph_observations")
    op.drop_index("idx_observations_project_type",
                  table_name="graph_observations")
    op.drop_index("idx_observations_type", table_name="graph_observations")
    op.drop_index("idx_observations_subject", table_name="graph_observations")
    op.drop_index("idx_observations_org", table_name="graph_observations")
    op.drop_index("idx_observations_project", table_name="graph_observations")

    # ── 3. Drop table ─────────────────────────────────────────────────────────
    op.drop_table("graph_observations")

    # Note: Row-level security on graph_observations is dropped as part of
    # the DROP TABLE (PostgreSQL automatically removes dependent policies).
