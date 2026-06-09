"""Add ``is_merged`` column to ``graph_entities`` for soft-delete dedup.

Enables the entity merge dedup worker (Track A, Phase 3c): non-canonical
duplicate entities are flagged with ``is_merged = True`` instead of being
hard-deleted, providing a 7-day recovery window before a separate GC process
removes them.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-08
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``is_merged`` column with a ``False`` default.

    Existing rows are implicitly backfilled to ``False`` by the server default.
    An index on ``(organization_id, is_merged)`` accelerates the merge worker's
    query for non-merged entities.
    """
    op.add_column(
        "graph_entities",
        sa.Column(
            "is_merged",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "idx_graph_entities_org_merged",
        "graph_entities",
        ["organization_id", "is_merged"],
    )


def downgrade() -> None:
    """Remove the ``is_merged`` column and its index.

    Rollback note:
        Any entities that were flagged as merged during the dedup run will
        retain their ``is_merged = True`` value in the application layer
        (but the column disappears).  A re-run of the migration forward would
        restore the column with all rows defaulting to ``False``.
    """
    op.drop_index("idx_graph_entities_org_merged", table_name="graph_entities")
    op.drop_column("graph_entities", "is_merged")
