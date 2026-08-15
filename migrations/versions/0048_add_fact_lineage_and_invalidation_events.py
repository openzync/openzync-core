"""Add fact lineage (superseded_by_fact_id) and the fact_invalidation_events table.

Two pieces backing fact-invalidation lineage:

- ``facts.superseded_by_fact_id``: self-referential FK pointing at the fact
  that superseded/invalidated this one.  NULL for retractions and expiry.
  ``ON DELETE SET NULL`` keeps a fact's lineage readable after its
  successor is hard-deleted (e.g. GDPR wipe).
- ``fact_invalidation_events``: append-only audit trail recording every
  way a fact stops being current — ``superseded`` (conflicting fact),
  ``retracted`` (manual), ``llm_invalidated`` (LLM-driven), or
  ``time_expired``.  A CHECK constraint enforces the four kinds; the
  ``organization_id`` column is denormalized (no FK) to mirror
  ``facts.organization_id`` for RLS.

Pure DDL with no backfill — existing facts keep ``superseded_by_fact_id``
NULL, which is the pre-feature behaviour.

Revision ID: 0048
Revises: 0047
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the lineage column to ``facts`` and create the events table."""
    op.add_column(
        "facts",
        sa.Column(
            "superseded_by_fact_id",
            sa.Uuid(),
            sa.ForeignKey(
                "facts.id",
                ondelete="SET NULL",
                name="fk_facts_superseded_by_fact_id",
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_facts_superseded_by_fact_id",
        "facts",
        ["superseded_by_fact_id"],
    )

    op.create_table(
        "fact_invalidation_events",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("old_fact_id", sa.Uuid(), nullable=False),
        sa.Column("new_fact_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("at_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_episode_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["old_fact_id"], ["facts.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["new_fact_id"], ["facts.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_episode_id"], ["episodes.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_fact_invalidation_events_project_id",
        "fact_invalidation_events",
        ["project_id"],
    )
    op.create_index(
        "ix_fact_invalidation_events_old_fact_id",
        "fact_invalidation_events",
        ["old_fact_id"],
    )
    op.create_index(
        "ix_fact_invalidation_events_new_fact_id",
        "fact_invalidation_events",
        ["new_fact_id"],
    )
    op.create_index(
        "ix_fact_invalidation_events_old_fact_id_at_time",
        "fact_invalidation_events",
        ["old_fact_id", "at_time"],
    )
    op.create_check_constraint(
        "ck_fact_invalidation_events_kind",
        "fact_invalidation_events",
        "kind IN ('superseded', 'retracted', 'llm_invalidated', 'time_expired')",
    )


def downgrade() -> None:
    """Drop the events table, then the lineage column, index, and FK.

    Rollback note: run ``alembic downgrade -1`` with the same
    ``OZ_DATABASE_URL`` used for the upgrade.  Any ``fact_invalidation_events``
    rows written since the upgrade are dropped with the table.
    """
    op.drop_table("fact_invalidation_events")
    op.drop_constraint(
        "fk_facts_superseded_by_fact_id", "facts", type_="foreignkey",
    )
    op.drop_index("ix_facts_superseded_by_fact_id", table_name="facts")
    op.drop_column("facts", "superseded_by_fact_id")
