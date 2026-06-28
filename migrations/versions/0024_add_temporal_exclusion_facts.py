"""Replace facts unique constraint with temporal exclusion constraint.

The existing ``uq_facts_episode_triple`` (btree unique on
``source_episode_id, subject, predicate, object``) allowed overlapping
time ranges for the same triple — two facts from the same episode could
have ``valid_from``..``valid_to`` ranges that overlapped, which is
semantically wrong.

The replacement is a **GiST exclusion constraint** powered by
``btree_gist``:

    EXCLUDE USING gist (
        source_episode_id WITH =,
        subject WITH =,
        predicate WITH =,
        "object" WITH =,
        tstzrange(COALESCE(valid_from, '-infinity'),
                   COALESCE(valid_to, 'infinity'), '[)') WITH &&
    ) WHERE (invalid_at IS NULL)

This prevents inserting a second fact for the same triple if its valid
range overlaps with an existing (non-invalidated) fact's range.

``COALESCE(valid_from, '-infinity')`` and
``COALESCE(valid_to, 'infinity')`` ensure that NULL bounds behave
correctly in the range comparison — a fact with ``valid_to = NULL`` is
treated as ``infinity`` (still active), and a fact with
``valid_from = NULL`` is treated as ``-infinity`` (has always been
valid).

Pre-migration integrity check (run manually before deploying):
    Zero overlapping rows found on the dev database — no dedup needed.

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Enable btree_gist extension ──────────────────────────────────────
    # Required for `=` (equality) operators on scalar columns inside
    # a GiST index.  Standard Postgres extension, available on all
    # RDS versions since 9.x, no side effects.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # ── 2. Drop the old unique constraint ────────────────────────────────────
    # Safe because we are replacing it with a strictly stronger constraint.
    op.drop_constraint("uq_facts_episode_triple", "facts", type_="unique")

    # ── 3. Add temporal exclusion constraint ────────────────────────────────
    # Prevents overlapping valid ranges for the same triple within the
    # same episode.  NULL valid_from → -infinity, NULL valid_to → infinity
    # so that open-ended ranges are handled correctly.
    op.execute(
        """
        ALTER TABLE facts
        ADD CONSTRAINT uq_facts_temporal_excl
        EXCLUDE USING gist (
            source_episode_id WITH =,
            subject WITH =,
            predicate WITH =,
            "object" WITH =,
            tstzrange(COALESCE(valid_from, '-infinity'),
                       COALESCE(valid_to, 'infinity'), '[)') WITH &&
        )
        WHERE (invalid_at IS NULL)
        """
    )


def downgrade() -> None:
    # ── 1. Drop exclusion constraint ────────────────────────────────────────
    # Alembic's ``op.drop_constraint`` does not support ``type_="exclude"``,
    # so we use raw SQL for the exclusion constraint in both directions.
    op.execute(
        "ALTER TABLE facts DROP CONSTRAINT IF EXISTS uq_facts_temporal_excl"
    )

    # ── 2. Restore original unique constraint ────────────────────────────────
    op.create_unique_constraint(
        "uq_facts_episode_triple",
        "facts",
        ["source_episode_id", "subject", "predicate", "object"],
    )

    # Note: ``btree_gist`` extension is intentionally NOT dropped.
    # It is harmless to leave installed and may be in use by other
    # indexes or constraints in the database.
