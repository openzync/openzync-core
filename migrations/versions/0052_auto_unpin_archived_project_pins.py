"""Auto-unpin archived project pins (data backfill).

Clears orphan pins left on already-archived projects. No schema change.
Pins are a reconstructible UI preference — downgrade is a no-op.

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM project_pins WHERE project_id IN "
        "(SELECT id FROM projects WHERE is_archived = true)"
    )


def downgrade() -> None:
    # No-op: pins are reconstructible UI prefs, no data to restore.
    pass
