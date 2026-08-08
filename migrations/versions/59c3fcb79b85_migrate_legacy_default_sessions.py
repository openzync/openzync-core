"""Migrate legacy ``__default__`` sessions to real external_ids.

Before ``session_id`` became required on ingestion, the system auto-created a
session with ``external_id = '__default__'`` per project.  Legacy rows may
still exist in production.  Rather than purging that data, each legacy
session is migrated to a NEW session row:

- All columns are copied (organization_id, project_id, user_id, metadata,
  is_active, is_deleted, closed_at, created_at, updated_at) with a fresh
  UUID ``id`` and ``external_id = 'migrated-<old_session_uuid>'`` — the old
  id is embedded in the new external_id, so the mapping is deterministic
  (unique against ``uq_session_project_external``) AND recoverable by
  downgrade.
- Dependents (episodes, episode_blobs, structured_extractions, and the
  FK-less ingest_dedup claims) are repointed to the new session id.
- The old ``__default__`` shell is deleted — empty of dependents at that
  point, so the FK ``ON DELETE CASCADE`` constraints are not exercised.

Facts are intentionally NOT touched — they are denormalized to
``project_id`` and never reference ``sessions.id``.

RLS context: the tables involved carry row-level security policies keyed on
``app.bypass_rls``/``app.org_id`` (migrations/0001).  The migration sets
``app.bypass_rls = 'true'`` session-locally so the DML is not silently
filtered when the migration role is not the table owner.

Revision ID: 59c3fcb79b85
Revises: c3a1b2f4d5e6
Create Date: 2026-08-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "59c3fcb79b85"
down_revision: str | None = "c3a1b2f4d5e6"
branch_labels: str | None = None
depends_on: str | None = None

# Matches exactly one UUID: 8-4-4-4-12 hex.  Used on downgrade to identify
# migration-created sessions and recover the original ``__default__`` id.
_UUID_TEXT_PATTERN = (
    r"^migrated-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def upgrade() -> None:
    """Copy each legacy ``__default__`` session, repoint dependents, delete shell.

    All statements run inside Alembic's single transaction — a failure
    mid-way rolls everything back, so partial migrations cannot leak
    orphaned rows or half-repointed dependents.
    """
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")

    # 1. Materialize old → new id map for every live legacy session.
    op.execute(
        sa.text(
            """
            CREATE TEMP TABLE _default_session_map ON COMMIT DROP AS
            SELECT
                s.id AS old_id,
                gen_random_uuid() AS new_id
            FROM sessions s
            WHERE s.external_id = '__default__'
              AND s.is_deleted = FALSE
            """
        )
    )

    # 2. Copy each legacy session to a fresh row with the mapped id and a
    #    deterministic external_id that encodes the old id.
    op.execute(
        sa.text(
            """
            INSERT INTO sessions (
                id, organization_id, project_id, user_id, external_id,
                metadata, is_active, is_deleted, closed_at, created_at,
                updated_at
            )
            SELECT
                m.new_id,
                s.organization_id,
                s.project_id,
                s.user_id,
                'migrated-' || s.id::text,
                s.metadata,
                s.is_active,
                s.is_deleted,
                s.closed_at,
                s.created_at,
                s.updated_at
            FROM sessions s
            JOIN _default_session_map m ON m.old_id = s.id
            """
        )
    )

    # 3. Repoint every dependent to the migrated session.  ingest_dedup has
    #    no FK — repointing anyway keeps dedup claims consistent with the
    #    episodes they deduplicated.
    op.execute(
        sa.text(
            """
            UPDATE episodes e
            SET session_id = m.new_id
            FROM _default_session_map m
            WHERE e.session_id = m.old_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE episode_blobs b
            SET session_id = m.new_id
            FROM _default_session_map m
            WHERE b.session_id = m.old_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE structured_extractions x
            SET session_id = m.new_id
            FROM _default_session_map m
            WHERE x.session_id = m.old_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ingest_dedup d
            SET session_id = m.new_id
            FROM _default_session_map m
            WHERE d.session_id = m.old_id
            """
        )
    )

    # 4. Delete the now-empty shell.  Dependents were repointed in step 3,
    #    so this row is unreferenced and the FK constraints are satisfied.
    op.execute(
        sa.text(
            """
            DELETE FROM sessions s
            USING _default_session_map m
            WHERE s.id = m.old_id
            """
        )
    )

    op.execute("DROP TABLE _default_session_map")


def downgrade() -> None:
    """Reverse the migration: repoint dependents back, delete migrated rows.

    The original ``__default__`` shell id is recovered from the migrated
    ``external_id`` (``migrated-<uuid>``), so the shell is fully restored
    with its original id — this is a complete reversal, not a best-effort.

    Limitation: only sessions whose external_id matches the migration's
    ``migrated-<uuid>`` pattern are reversed.  A user-created session that
    happens to use that exact prefix would also be reverted; the strict
    UUID pattern makes a false positive practically impossible.
    """
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")

    # 1. Recover old → new id map from the deterministic external_id.
    op.execute(
        sa.text(
            """
            CREATE TEMP TABLE _default_session_map ON COMMIT DROP AS
            SELECT
                substring(s.external_id FROM 10)::uuid AS old_id,
                s.id AS new_id
            FROM sessions s
            WHERE s.external_id ~ :pattern
            """
        ).bindparams(pattern=_UUID_TEXT_PATTERN)
    )

    # 2. Recreate the ``__default__`` shell with its original id BEFORE
    #    repointing dependents — the episode/blob/extraction FKs validate
    #    ``session_id`` against ``sessions.id`` at UPDATE time, so the
    #    target row must already exist.
    op.execute(
        sa.text(
            """
            INSERT INTO sessions (
                id, organization_id, project_id, user_id, external_id,
                metadata, is_active, is_deleted, closed_at, created_at,
                updated_at
            )
            SELECT
                m.old_id,
                s.organization_id,
                s.project_id,
                s.user_id,
                '__default__',
                s.metadata,
                s.is_active,
                s.is_deleted,
                s.closed_at,
                s.created_at,
                s.updated_at
            FROM sessions s
            JOIN _default_session_map m ON m.new_id = s.id
            """
        )
    )

    # 3. Repoint dependents back to the restored shell ids.
    op.execute(
        sa.text(
            """
            UPDATE episodes e
            SET session_id = m.old_id
            FROM _default_session_map m
            WHERE e.session_id = m.new_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE episode_blobs b
            SET session_id = m.old_id
            FROM _default_session_map m
            WHERE b.session_id = m.new_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE structured_extractions x
            SET session_id = m.old_id
            FROM _default_session_map m
            WHERE x.session_id = m.new_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ingest_dedup d
            SET session_id = m.old_id
            FROM _default_session_map m
            WHERE d.session_id = m.new_id
            """
        )
    )

    # 4. Delete the migrated session rows.
    op.execute(
        sa.text(
            """
            DELETE FROM sessions s
            USING _default_session_map m
            WHERE s.id = m.new_id
            """
        )
    )

    op.execute("DROP TABLE _default_session_map")
