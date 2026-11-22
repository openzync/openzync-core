"""Restore dialog_classifications dedup constraint on the new owner column.

0069 dropped ``uq_dialog_classifications_org_episode`` with the
``organization_id`` column but added no replacement, so the
``ON CONFLICT (user_id, episode_id) DO NOTHING`` upsert in
``workers.tasks.classify_dialog.process_classification_output`` has no
arbiter and every classification insert fails.  This revision deduplicates
by ``(user_id, episode_id)`` (keeping the most recent row per pair, same
rule as 0040) and adds ``uq_dialog_classifications_user_episode`` — the
name the ORM model already declares, so autogenerate diffs stay clean.

No ``CONCURRENTLY``: ``CREATE INDEX CONCURRENTLY`` cannot run inside the
transaction Alembic holds, and the table is small and write-gated by the
enrichment worker — a plain ``ADD CONSTRAINT`` is the right call.  The
RLS bypass below mirrors 0069: with ``FORCE ROW LEVEL SECURITY`` a
context-less session matches no rows, so the dedupe DELETE would silently
do nothing and the constraint build would then fail on the surviving
duplicates.

Revision ID: 0071
Revises: 0070
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # RLS is forced on this table — bypass so the dedupe sees every row.
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")
    # Deduplicate before creating the constraint — the crash-loop this
    # revision fixes may already have produced duplicate rows.
    op.execute("""
        DELETE FROM dialog_classifications
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY user_id, episode_id
                    ORDER BY updated_at DESC NULLS LAST, created_at DESC
                ) AS rn
                FROM dialog_classifications
            ) sub
            WHERE rn > 1
        )
    """)
    op.execute(
        "ALTER TABLE dialog_classifications DROP CONSTRAINT IF EXISTS "
        "uq_dialog_classifications_user_episode"
    )
    op.execute(
        "ALTER TABLE dialog_classifications ADD CONSTRAINT "
        "uq_dialog_classifications_user_episode UNIQUE (user_id, episode_id)"
    )


def downgrade() -> None:
    """Drop the restored dedup constraint.

    Rollback note: run ``alembic downgrade -1`` with the same
    ``OZ_DATABASE_URL`` used for the upgrade. Rows deduplicated by the
    upgrade are not restored.
    """
    op.execute(
        "ALTER TABLE dialog_classifications DROP CONSTRAINT IF EXISTS "
        "uq_dialog_classifications_user_episode"
    )
