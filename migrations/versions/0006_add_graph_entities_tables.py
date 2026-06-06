"""Add graph_entities, graph_relationships, graph_episode_entities tables.

Implements the PostgreSQL-native graph backend schema replacing Graphiti.
See docs/implementation/04-knowledge-graph/06-postgres-graph-backend.md.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the three graph tables with indexes for BFS and search.

    Tables created:
        * ``graph_entities`` — entity nodes (people, orgs, products, etc.)
        * ``graph_relationships`` — directed temporal edges between entities
        * ``graph_episode_entities`` — join table linking episodes to entities
    """
    # Enable required extensions (idempotent)
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ── graph_entities ────────────────────────────────────────────────────
    op.create_table(
        "graph_entities",
        sa.Column("id", sa.UUID(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False,
                  server_default="custom"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("attributes", postgresql.JSONB(), nullable=False,
                  server_default="{}"),
        sa.Column("embedding", sa.ARRAY(sa.Float()), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            ondelete="CASCADE",
        ),
    )
    # Tenant isolation
    op.create_index("idx_graph_entities_org", "graph_entities",
                    ["organization_id"])
    # Type filtering
    op.create_index("idx_graph_entities_type", "graph_entities",
                    ["entity_type"])
    # Fuzzy name search
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_entities_name_trgm "
        "ON graph_entities USING GIN (name gin_trgm_ops)"
    )
    # Full-text search on summaries
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_entities_summary_fts "
        "ON graph_entities USING GIN "
        "(to_tsvector('english', coalesce(summary, '')))"
    )

    # ── graph_relationships ───────────────────────────────────────────────
    op.create_table(
        "graph_relationships",
        sa.Column("id", sa.UUID(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("relationship_type", sa.Text(), nullable=False),
        sa.Column("properties", postgresql.JSONB(), nullable=False,
                  server_default="{}"),
        sa.Column("fact", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False,
                  server_default="1.0"),
        sa.Column("source_episode_id", sa.UUID(), nullable=True),
        sa.Column("valid_from", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("valid_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("invalid_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["graph_entities.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["graph_entities.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_episode_id"], ["episodes.id"], ondelete="SET NULL",
        ),
    )
    # Tenant isolation
    op.create_index("idx_graph_rels_org", "graph_relationships",
                    ["organization_id"])
    # BFS traversal: find all edges from/to a node (with org scope)
    op.create_index("idx_graph_rels_source_org", "graph_relationships",
                    ["source_id", "organization_id"])
    op.create_index("idx_graph_rels_target_org", "graph_relationships",
                    ["target_id", "organization_id"])
    # Filter by relationship type
    op.create_index("idx_graph_rels_type", "graph_relationships",
                    ["relationship_type"])
    # Temporal queries: find facts active at a point in time
    op.create_index("idx_graph_rels_valid", "graph_relationships",
                    ["valid_from", "valid_to"])
    # Partial indexes for active (non-invalidated) edges — speeds up BFS
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_rels_source_active "
        "ON graph_relationships(source_id) WHERE invalid_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_rels_target_active "
        "ON graph_relationships(target_id) WHERE invalid_at IS NULL"
    )
    # Prevent duplicate active relationships between same entities
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_rels_active_unique "
        "ON graph_relationships(source_id, target_id, relationship_type) "
        "WHERE invalid_at IS NULL"
    )

    # ── graph_episode_entities ────────────────────────────────────────────
    op.create_table(
        "graph_episode_entities",
        sa.Column("episode_id", sa.UUID(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["episode_id"], ["episodes.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["graph_entities.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("episode_id", "entity_id"),
    )
    op.create_index("idx_graph_ep_entity", "graph_episode_entities",
                    ["entity_id"])


def downgrade() -> None:
    """Drop all three graph tables in reverse dependency order."""
    op.drop_table("graph_episode_entities")
    op.drop_table("graph_relationships")
    op.drop_table("graph_entities")
