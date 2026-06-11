"""Add unique constraint on graph_entities(organization_id, name).

Before creating the constraint, deduplicates existing entities by keeping
only the most recently created row per (organization_id, name) pair.

Revision ID: e73169ac1c70
Revises: 0011
Create Date: 2026-06-11 08:54:32.382177
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "e73169ac1c70"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Deduplicate ─────────────────────────────────────────────────────
    # Keep only the most recent row per (org_id, name).  The subquery finds
    # the latest created_at for each group; the DELETE removes anything older.
    op.execute(
        """
        DELETE FROM graph_entities ge
        WHERE ge.id NOT IN (
            SELECT DISTINCT ON (g.organization_id, g.name) g.id
            FROM graph_entities g
            ORDER BY g.organization_id, g.name, g.created_at DESC
        )
        """
    )

    # ── 2. Add unique constraint ───────────────────────────────────────────
    op.create_unique_constraint(
        "uq_graph_entities_org_name",
        "graph_entities",
        ["organization_id", "name"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_graph_entities_org_name", "graph_entities", type_="unique")
