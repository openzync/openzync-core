"""Add ``type`` column to ``extraction_schemas`` for schema categorization.

This enables the classification pipeline: schemas with ``type='classification'``
store label definitions (intent, emotion, valence, arousal) that the
:func:`workers.tasks.classify_dialog` worker reads to configure the LLM prompt.

Revision ID: 0007
Revises: 99a299513d53
Create Date: 2026-06-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "99a299513d53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add type column with a default of 'structured' so existing rows are backfilled.
    op.add_column(
        "extraction_schemas",
        sa.Column("type", sa.String(50), nullable=False, server_default="structured"),
    )
    # Create a composite index for filtering schemas by org + type — the primary
    # query pattern for the classification worker.
    op.create_index(
        "ix_extraction_schemas_org_type",
        "extraction_schemas",
        ["organization_id", "type"],
    )
    # Explicitly backfill any rows where type might still be NULL (defensive —
    # server_default already handles new rows, but pre-existing NULLs from schema
    # changes before this migration need coverage).
    op.execute(
        "UPDATE extraction_schemas SET type = 'structured' WHERE type IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_extraction_schemas_org_type")
    op.drop_column("extraction_schemas", "type")
