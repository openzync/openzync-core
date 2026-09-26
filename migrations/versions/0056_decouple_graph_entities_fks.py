"""Decouple the graph path from the ``graph_entities`` stub table.

Drops the four foreign keys that point at ``graph_entities``:

- ``facts.subject_entity_id`` → ``fk_facts_subject_entity`` (from 0011)
- ``facts.object_entity_id`` → ``fk_facts_object_entity`` (from 0011)
- ``graph_observations.subject_entity_id`` →
  ``graph_observations_subject_entity_id_fkey`` (from 0025, auto-named)
- ``graph_observations.related_entity_id`` →
  ``graph_observations_related_entity_id_fkey`` (from 0025, auto-named)

The UUID columns and their indexes are kept — only the constraints go.
Upgrade is safe (all rows kept); the FalkorDB/SurrealDB paths never wrote
the stub table, so no join rows reference it.

Downgrade is NOT clean: once orphan UUIDs accumulate in the decoupled
columns, re-creating the foreign keys fails on the orphans.  The
``downgrade()`` below restores the constraints for schema completeness,
but it only succeeds on databases whose columns contain no orphans.

Revision ID: 0056
Revises: 0055
Create Date: 2026-09-26
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop FK constraints to graph_entities; keep UUID columns and rows."""
    op.drop_constraint("fk_facts_subject_entity", "facts", type_="foreignkey")
    op.drop_constraint("fk_facts_object_entity", "facts", type_="foreignkey")
    op.drop_constraint(
        "graph_observations_subject_entity_id_fkey",
        "graph_observations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "graph_observations_related_entity_id_fkey",
        "graph_observations",
        type_="foreignkey",
    )


def downgrade() -> None:
    """Re-create the FK constraints (fails if orphan UUIDs accumulated)."""
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
    op.create_foreign_key(
        "graph_observations_subject_entity_id_fkey",
        "graph_observations",
        "graph_entities",
        ["subject_entity_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "graph_observations_related_entity_id_fkey",
        "graph_observations",
        "graph_entities",
        ["related_entity_id"],
        ["id"],
        ondelete="SET NULL",
    )
