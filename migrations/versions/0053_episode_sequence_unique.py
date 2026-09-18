"""Backfill colliding episode sequence numbers + unique seq per session.

Concurrent ingests previously computed ``MAX(sequence_number) + 1`` without
a row lock, so duplicate ``(session_id, sequence_number)`` pairs exist in
production.  This migration:

1. Bumps colliding live rows to ``MAX+1, MAX+2, ...`` — earliest
   ``(created_at, id)`` keeps its number, later duplicates are renumbered
   in ``(created_at, id)`` order.
2. Adds partial unique index ``uq_episodes_session_sequence`` on
   ``(session_id, sequence_number) WHERE is_deleted = false`` — the final
   guard for the locked ``MAX+1`` protocol in ``MemoryService.ingest``.
   Partial (not full UNIQUE) so a soft-deleted row never blocks reuse of
   its number, matching ``get_next_sequence`` which scans live rows only.

Raw SQL + Alembic Python together (repo convention: raw SQL via op.execute
for the data step, op.create_index for the schema step).

Rollback: the index is dropped.  The renumbering is NOT reversed —
colliding numbers have no canonical original order beyond the bump, and
re-colliding rows would violate nothing but reintroduce the bug.  The
downgrade is therefore index-only (documented, not silent).

Revision ID: 0053
Revises: 0052
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence  # noqa: TC003

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Renumber duplicate live seq values, then enforce uniqueness."""
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")

    # Bumping colliding rows to MAX+1 ordered by created_at, id: within
    # each (session_id, sequence_number) group the earliest row keeps its
    # number (rn = 1); every later duplicate gets max_seq + row_number,
    # which is guaranteed above every existing live number in the session.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    session_id,
                    created_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY session_id, sequence_number
                        ORDER BY created_at, id
                    ) AS rn
                FROM episodes
                WHERE is_deleted = false
            ),
            maxseq AS (
                SELECT
                    session_id,
                    COALESCE(MAX(sequence_number), -1) AS max_seq
                FROM episodes
                WHERE is_deleted = false
                GROUP BY session_id
            ),
            collisions AS (
                SELECT
                    r.id,
                    (
                        m.max_seq
                        + ROW_NUMBER() OVER (
                            PARTITION BY r.session_id
                            ORDER BY r.created_at, r.id
                        )
                    ) AS new_seq
                FROM ranked r
                JOIN maxseq m ON m.session_id = r.session_id
                WHERE r.rn > 1
            )
            UPDATE episodes e
            SET sequence_number = c.new_seq,
                updated_at = now()
            FROM collisions c
            WHERE e.id = c.id
            """
        )
    )

    op.create_index(
        "uq_episodes_session_sequence",
        "episodes",
        ["session_id", "sequence_number"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )


def downgrade() -> None:
    """Drop the unique index.  Data renumbering is intentionally not reversed."""
    op.drop_index(
        "uq_episodes_session_sequence",
        table_name="episodes",
    )
