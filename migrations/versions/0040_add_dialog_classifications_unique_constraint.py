"""Add unique constraint on dialog_classifications (org, episode).

Prevents duplicate classification rows when enrich_episode runs
concurrently for the same episode.

Revision ID: 0040
Revises: 0039
Create Date: 2026-07-26
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Deduplicate before creating the constraint — if the race condition
    # we are fixing already produced duplicate rows, this migration would
    # fail.  Keep the most recent row per (org_id, episode_id).
    op.execute("""
        DELETE FROM dialog_classifications
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY organization_id, episode_id
                    ORDER BY updated_at DESC NULLS LAST, created_at DESC
                ) AS rn
                FROM dialog_classifications
            ) sub
            WHERE rn > 1
        )
    """)
    op.create_unique_constraint(
        "uq_dialog_classifications_org_episode",
        "dialog_classifications",
        ["organization_id", "episode_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_dialog_classifications_org_episode",
        "dialog_classifications",
        type_="unique",
    )
