"""add updated_at to graph_relationships

Revision ID: 2089cbf56394
Revises: e73169ac1c70
Create Date: 2026-06-11 17:05:35.808704
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2089cbf56394'
down_revision: Union[str, None] = 'e73169ac1c70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "graph_relationships",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("graph_relationships", "updated_at")
