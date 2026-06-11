"""Add ``subject_entity_id`` and ``object_entity_id`` to ``facts``.

Adds two nullable foreign key columns that link facts to graph entities,
enabling entity-aware fact queries (e.g., "all facts about Rohan") and
pronoun resolution in the fact extraction pipeline.

Both columns are NULLable — existing facts continue to work unchanged.
New facts extracted after this migration will optionally set these IDs.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add entity ID columns and FKs to graph_entities."""
    op.add_column(
        "facts",
        sa.Column(
            "subject_entity_id",
            sa.UUID(),
            nullable=True,
            comment="FK to graph_entities — resolved entity for the subject.",
        ),
    )
    op.add_column(
        "facts",
        sa.Column(
            "object_entity_id",
            sa.UUID(),
            nullable=True,
            comment="FK to graph_entities — resolved entity for the object.",
        ),
    )
    op.create_index(
        "ix_facts_subject_entity",
        "facts",
        ["subject_entity_id"],
    )
    op.create_index(
        "ix_facts_object_entity",
        "facts",
        ["object_entity_id"],
    )
    op.create_foreign_key(
        "fk_facts_subject_entity",
        "facts",
        "graph_entities",
        ["subject_entity_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_facts_object_entity",
        "facts",
        "graph_entities",
        ["object_entity_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Remove entity ID columns and FKs."""
    op.drop_constraint("fk_facts_subject_entity", "facts", type_="foreignkey")
    op.drop_constraint("fk_facts_object_entity", "facts", type_="foreignkey")
    op.drop_index("ix_facts_subject_entity", table_name="facts")
    op.drop_index("ix_facts_object_entity", table_name="facts")
    op.drop_column("facts", "subject_entity_id")
    op.drop_column("facts", "object_entity_id")
