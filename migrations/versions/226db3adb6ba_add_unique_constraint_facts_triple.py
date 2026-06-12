"""Add unique constraint on facts(source_episode_id, subject, predicate, object).

Prevents duplicate fact triples from different workers writing to the same
episode (extract_entities step 9 vs extract_facts).  Before creating the
constraint, deduplicates existing rows by keeping only the most recently
created row per (source_episode_id, subject, predicate, object) group.

Revision ID: 226db3adb6ba
Revises: 2089cbf56394
Create Date: 2026-06-11
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "226db3adb6ba"
down_revision: Union[str, None] = "5d21420b9c05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Deduplicate existing rows — keep the most recent per triple ─────
    op.execute(
        """
        DELETE FROM facts f
        WHERE f.id NOT IN (
            SELECT DISTINCT ON (source_episode_id, subject, predicate, "object") f2.id
            FROM facts f2
            WHERE f2.subject IS NOT NULL
              AND f2.predicate IS NOT NULL
              AND f2."object" IS NOT NULL
              AND f2.source_episode_id IS NOT NULL
            ORDER BY source_episode_id, subject, predicate, "object", f2.created_at DESC
        )
        """
    )

    # ── 2. Add unique constraint ───────────────────────────────────────────
    op.create_unique_constraint(
        "uq_facts_episode_triple",
        "facts",
        ["source_episode_id", "subject", "predicate", "object"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_facts_episode_triple", "facts", type_="unique")
