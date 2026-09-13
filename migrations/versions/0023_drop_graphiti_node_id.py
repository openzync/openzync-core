"""Drop graphiti_node_id column from episodes.

The ``graphiti_node_id`` column stored FalkorDB node references that are
no longer needed after removing the graphiti-core dependency.  All code
references to this column were removed in earlier phases of the cleanup
(Phases 5 and 8 of the remove-from-graphiti plan).

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("episodes", "graphiti_node_id")


def downgrade() -> None:
    op.add_column(
        "episodes",
        sa.Column("graphiti_node_id", sa.Text(), nullable=True),
    )
