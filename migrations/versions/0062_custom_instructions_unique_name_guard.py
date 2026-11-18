"""Backfill-guard the custom_instructions unique name index.

Ensures at most one instruction per ``name`` within each
``(organization_id, scope, target_id)`` group.  Migration ``0013`` already
created this functional unique index
(``ix_custom_instructions_unique_scope_name``, with ``COALESCE`` so
org-level ``target_id IS NULL`` rows are covered), so on a normally
migrated database the dedupe below collapses zero rows and the
``IF NOT EXISTS`` index creation is a no-op.  This migration exists so
databases with out-of-band rows (manual inserts, restored backups taken
before ``0013``) converge deterministically instead of failing loudly
at some later write.

Handling note for existing dupes: duplicates are collapsed
deterministically by keeping the earliest row per group (lowest
``created_at``, tie-broken by lowest ``id``) and deleting the rest.
New writes can no longer create dupes: ``set_by_scope`` rejects
duplicate names with ``ValidationError`` (422) before delete+insert.

Revision ID: 0062
Revises: 0061
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Collapse any duplicate names per (org, scope, target) group,
    # keeping the earliest row.  Deterministic: ORDER BY created_at, id.
    op.execute("""
        DELETE FROM custom_instructions
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY organization_id, scope,
                        COALESCE(target_id,
                            '00000000-0000-0000-0000-000000000000'),
                        name
                    ORDER BY created_at ASC NULLS LAST, id ASC
                ) AS rn
                FROM custom_instructions
            ) sub
            WHERE rn > 1
        )
    """)
    # Idempotent: 0013 already created this exact index on migrated DBs.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_custom_instructions_unique_scope_name
        ON custom_instructions (
            organization_id,
            scope,
            COALESCE(target_id, '00000000-0000-0000-0000-000000000000'),
            name
        )
    """)


def downgrade() -> None:
    """No-op: the unique index predates this migration (created in 0013).

    Dropping it here would break 0013's contract on downgrade chains,
    so rollback intentionally leaves the index in place.
    """
