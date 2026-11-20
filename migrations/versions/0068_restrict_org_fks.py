"""Restrict platform-org delete across all org-scoped FKs.

Every ``organization_id`` FK that references ``organizations(id)`` becomes
``ON DELETE RESTRICT`` (``ON UPDATE CASCADE`` from 0065 is preserved — org
ids never change in app code, and the survivor PK move relied on it).

There is no ``DELETE /organizations`` endpoint in ``routers/`` — the
platform org is never deleted via the API.  RESTRICT is defence-in-depth
so a stray SQL delete cannot wipe tenant rows.

Scope note: only tables with a real FK to ``organizations`` are touched.
Tables in 0065's ``ORG_SCOPED_TABLES`` without such a constraint
(``facts``, ``fact_invalidation_events``, ``graph_entities``,
``graph_observations``, ``structured_extractions``, ``llm_usage`` —
denormalized ``organization_id`` with no FK, plus ``audit_logs`` whose
``organization_id`` is nullable with no FK) are no-ops here by construction.

Order note: 0065 step 4 guards on ``to_regclass('instance_settings')``
because 0066 creates that table yet is ordered *after* 0065 — the guard
is intentional, not a missing dependency.

Revision ID: 0068
Revises: 0067
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror of 0065 ORG_SCOPED_TABLES (users already RESTRICT via 0067; the
# no-FK tables below are scanned and skipped — no error when absent).
ORG_TABLES: tuple[str, ...] = (
    "users",
    "projects",
    "project_pins",
    "api_keys",
    "webhook_endpoints",
    "sessions",
    "episodes",
    "episode_blobs",
    "facts",
    "fact_invalidation_events",
    "graph_entities",
    "graph_observations",
    "custom_instructions",
    "extraction_schemas",
    "dialog_classifications",
    "structured_extractions",
    "audit_logs",
    "refresh_tokens",
    "llm_usage",
)


def upgrade() -> None:
    """Recreate every organizations FK with ``ON DELETE RESTRICT``."""
    tables = ", ".join(f"'{t}'" for t in ORG_TABLES)
    op.execute(
        f"""
        DO $$
        DECLARE
            r RECORD;
            def TEXT;
        BEGIN
            FOR r IN
                SELECT con.oid, con.conname, tbl.relname AS tablename
                FROM pg_constraint con
                JOIN pg_class tbl ON tbl.oid = con.conrelid
                JOIN pg_class ref ON ref.oid = con.confrelid
                JOIN pg_namespace nsp ON nsp.oid = tbl.relnamespace
                WHERE con.contype = 'f'
                  AND ref.relname = 'organizations'
                  AND tbl.relname IN ({tables})
                  AND nsp.nspname = 'public'
            LOOP
                def := pg_get_constraintdef(r.oid);
                def := regexp_replace(def, ' ON DELETE [A-Z ]+', '', 'i');
                def := regexp_replace(def, ' ON UPDATE [A-Z ]+', '', 'i');
                EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', r.tablename, r.conname);
                EXECUTE format(
                    'ALTER TABLE %I ADD CONSTRAINT %I %s '
                    'ON DELETE RESTRICT ON UPDATE CASCADE',
                    r.tablename, r.conname, def
                );
            END LOOP;
        END $$;
        """
    )


def downgrade() -> None:
    """Restore ``ON DELETE CASCADE`` on the organizations FKs.

    Rollback note: run ``alembic downgrade -1`` with the same
    ``OZ_DATABASE_URL`` used for the upgrade.  Only the delete action
    changes — no data is touched by the rollback itself.
    """
    tables = ", ".join(f"'{t}'" for t in ORG_TABLES)
    op.execute(
        f"""
        DO $$
        DECLARE
            r RECORD;
            def TEXT;
        BEGIN
            FOR r IN
                SELECT con.oid, con.conname, tbl.relname AS tablename
                FROM pg_constraint con
                JOIN pg_class tbl ON tbl.oid = con.conrelid
                JOIN pg_class ref ON ref.oid = con.confrelid
                JOIN pg_namespace nsp ON nsp.oid = tbl.relnamespace
                WHERE con.contype = 'f'
                  AND ref.relname = 'organizations'
                  AND tbl.relname IN ({tables})
                  AND nsp.nspname = 'public'
            LOOP
                def := pg_get_constraintdef(r.oid);
                def := regexp_replace(def, ' ON DELETE [A-Z ]+', '', 'i');
                def := regexp_replace(def, ' ON UPDATE [A-Z ]+', '', 'i');
                EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', r.tablename, r.conname);
                EXECUTE format(
                    'ALTER TABLE %I ADD CONSTRAINT %I %s '
                    'ON DELETE CASCADE ON UPDATE CASCADE',
                    r.tablename, r.conname, def
                );
            END LOOP;
        END $$;
        """
    )
