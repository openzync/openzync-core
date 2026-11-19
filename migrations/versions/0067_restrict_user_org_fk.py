"""Restrict platform-org delete — users FK no longer cascades.

``users.organization_id`` was ``ON DELETE CASCADE``: deleting the
singleton platform org would wipe every user row in one statement.  The
platform org must never be deletable while users exist, so the FK becomes
``ON DELETE RESTRICT`` (``ON UPDATE CASCADE`` from 0065 is preserved —
org ids never change in app code, and the survivor PK move relied on it).

Revision ID: 0067
Revises: 0066
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Recreate the users→organizations FK with ``ON DELETE RESTRICT``."""
    op.execute(
        """
        DO $$
        DECLARE
            r RECORD;
            def TEXT;
        BEGIN
            FOR r IN
                SELECT con.oid, con.conname
                FROM pg_constraint con
                JOIN pg_class tbl ON tbl.oid = con.conrelid
                JOIN pg_class ref ON ref.oid = con.confrelid
                JOIN pg_namespace nsp ON nsp.oid = tbl.relnamespace
                WHERE con.contype = 'f'
                  AND tbl.relname = 'users'
                  AND ref.relname = 'organizations'
                  AND nsp.nspname = 'public'
            LOOP
                def := pg_get_constraintdef(r.oid);
                -- Strip any existing ON DELETE / ON UPDATE actions so the
                -- append below never duplicates them.
                def := regexp_replace(def, ' ON DELETE [A-Z ]+', '', 'i');
                def := regexp_replace(def, ' ON UPDATE [A-Z ]+', '', 'i');
                EXECUTE format('ALTER TABLE users DROP CONSTRAINT %I', r.conname);
                EXECUTE format(
                    'ALTER TABLE users ADD CONSTRAINT %I %s '
                    'ON DELETE RESTRICT ON UPDATE CASCADE',
                    r.conname, def
                );
            END LOOP;
        END $$;
        """
    )


def downgrade() -> None:
    """Restore ``ON DELETE CASCADE`` on the users FK.

    Rollback note: run ``alembic downgrade -1`` with the same
    ``OZ_DATABASE_URL`` used for the upgrade.  Only the delete action
    changes — no data is touched by the rollback itself.
    """
    op.execute(
        """
        DO $$
        DECLARE
            r RECORD;
            def TEXT;
        BEGIN
            FOR r IN
                SELECT con.oid, con.conname
                FROM pg_constraint con
                JOIN pg_class tbl ON tbl.oid = con.conrelid
                JOIN pg_class ref ON ref.oid = con.confrelid
                JOIN pg_namespace nsp ON nsp.oid = tbl.relnamespace
                WHERE con.contype = 'f'
                  AND tbl.relname = 'users'
                  AND ref.relname = 'organizations'
                  AND nsp.nspname = 'public'
            LOOP
                def := pg_get_constraintdef(r.oid);
                def := regexp_replace(def, ' ON DELETE [A-Z ]+', '', 'i');
                def := regexp_replace(def, ' ON UPDATE [A-Z ]+', '', 'i');
                EXECUTE format('ALTER TABLE users DROP CONSTRAINT %I', r.conname);
                EXECUTE format(
                    'ALTER TABLE users ADD CONSTRAINT %I %s '
                    'ON DELETE CASCADE ON UPDATE CASCADE',
                    r.conname, def
                );
            END LOOP;
        END $$;
        """
    )
