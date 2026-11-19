"""Singleton root org — elect a SYSTEM survivor, remap tenants, quarantine rest.

Single platform org (``00000000-0000-0000-0000-0000000000aa``):

- Normalize every FK referencing ``organizations(id)`` to
  ``ON UPDATE CASCADE`` first (step 0): the survivor PK move rewrites
  ``organizations.id`` while the survivor still owns child rows, and every
  FK here is ``ON DELETE CASCADE`` with no ``ON UPDATE`` action — the
  UPDATE would raise an FK violation. Harmless afterwards (org ids never
  change in app code); skipped on rerun for constraints already CASCADE.
- Elect the survivor: the org literally named ``SYSTEM`` when present,
  else the oldest ``approved`` org, else the oldest org of any kind.
  The platform row is upserted at the fixed ``PLATFORM_ORG_ID`` (named
  after the survivor so bootstrap naming is preserved).
- Remap every ``organization_id`` FK to the survivor (users, projects,
  api_keys, webhooks + delivery logs, sessions, episodes, facts, and all
  org-scoped satellite tables).  Email uniqueness is global (partial
  unique index), so no dedup is needed; pre-existing cross-org duplicate
  emails would already have violated that index.
- Quarantine non-survivor orgs: ``is_active=false``,
  ``status='rejected'``, name prefixed ``[QUARANTINED] `` — rows stay for
  the audit trail, RLS keeps them invisible to the tenant session.
- Backfill ``instance_settings.registration_mode='invite_only'`` for any
  row carrying a removed value, replace the CHECK constraint.
- Add ``users.config_override`` JSONB (per-user layer over org config).

BACKUP NOTE: take a ``pg_dump`` before running this migration on any
dataset with more than one org.  Steps 1-3 rewrite every org-scoped row
in one transaction and the remap/quarantine are intentionally
irreversible (pre-merge org attribution is not recoverable).  Step 2 is
batched (10k rows per statement) with per-table row-count notices.

Revision ID: 0065
Revises: 0064
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLATFORM_ORG_ID = "00000000-0000-0000-0000-0000000000aa"

# Every table carrying an ``organization_id`` column remapped to the survivor.
# ``ingest_dedup`` is intentionally absent: scoped by (project_id, session_id),
# it has no ``organization_id`` column — it inherits scope via its project.
# Same for ``project_members`` (via projects) and ``webhook_delivery_logs``
# (via webhook_endpoints): neither carries ``organization_id``.
ORG_SCOPED_TABLES: tuple[str, ...] = (
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
    """Elect survivor, remap FKs, quarantine the rest, backfill settings."""
    # RLS is FORCE + fail-closed (0059/0060): every DML below must bypass it,
    # same one-liner as 0044/0050/0053. Session-local; dies with the connection.
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")

    # ── 0. Normalize FKs to ON UPDATE CASCADE ──────────────────────────
    # Step 1 rewrites ``organizations.id`` while the survivor still owns
    # child rows — without this, the PK UPDATE violates every
    # ON DELETE CASCADE / NO ACTION FK. The cascade lets the move propagate
    # to the survivor's own children; step 2 then remaps all other orgs.
    # Idempotent: constraints already CASCADE are skipped. Pure DDL, so it
    # runs transactionally alongside the steps below.
    op.execute(
        """
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
                  AND nsp.nspname = 'public'
                  AND con.confupdtype <> 'c'
            LOOP
                def := pg_get_constraintdef(r.oid);
                -- No FK in this schema carries an explicit ON UPDATE clause,
                -- but strip one defensively so the append never duplicates it.
                def := regexp_replace(def, ' ON UPDATE [A-Z ]+', '', 'i');
                EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', r.tablename, r.conname);
                EXECUTE format(
                    'ALTER TABLE %I ADD CONSTRAINT %I %s ON UPDATE CASCADE',
                    r.tablename, r.conname, def
                );
            END LOOP;
        END $$;
        """
    )

    # ── 1. Elect the survivor ──────────────────────────────────────────
    op.execute(
        """
        DO $$
        DECLARE
            survivor_id UUID;
            survivor_name TEXT;
        BEGIN
            SELECT id, name INTO survivor_id, survivor_name
            FROM organizations
            WHERE name = 'SYSTEM'
            ORDER BY created_at ASC LIMIT 1;
            IF survivor_id IS NULL THEN
                SELECT id, name INTO survivor_id, survivor_name
                FROM organizations
                WHERE status = 'approved'
                ORDER BY created_at ASC LIMIT 1;
            END IF;
            IF survivor_id IS NULL THEN
                SELECT id, name INTO survivor_id, survivor_name
                FROM organizations
                ORDER BY created_at ASC LIMIT 1;
            END IF;

            IF survivor_id IS NULL THEN
                -- Fresh install: seed the platform row directly.
                INSERT INTO organizations
                    (id, name, status, config, llm_config, quotas,
                     is_active, org_code, join_enabled)
                VALUES
                    ('00000000-0000-0000-0000-0000000000aa', 'Platform',
                     'approved', '{}', '{}', '{}',
                     TRUE, 'BOOTSTRAP', FALSE)
                ON CONFLICT (id) DO NOTHING;
            ELSIF survivor_id <> '00000000-0000-0000-0000-0000000000aa'::uuid THEN
                -- Move the survivor onto the fixed platform id, then remap.
                UPDATE organizations SET id = '00000000-0000-0000-0000-0000000000aa'
                WHERE id = survivor_id;
            END IF;

            -- Survivor is live under its (possibly bootstrap) name.
            UPDATE organizations
            SET status = 'approved', is_active = TRUE
            WHERE id = '00000000-0000-0000-0000-0000000000aa';
        END $$;
        """
    )

    # ── 2. Remap every org-scoped FK to the survivor ────────────────────
    # Batched (10k rows per UPDATE) with per-table row-count notices:
    # one giant UPDATE would hold every row lock to commit and bloat the
    # single transaction on large tenants tables (episodes, facts).
    for table in ORG_SCOPED_TABLES:
        op.execute(
            f"""
            DO $$
            DECLARE
                moved INT;
                total INT := 0;
            BEGIN
                LOOP
                    UPDATE {table} SET organization_id = '{PLATFORM_ORG_ID}'
                    WHERE ctid IN (
                        SELECT ctid FROM {table}
                        WHERE organization_id <> '{PLATFORM_ORG_ID}'
                        LIMIT 10000
                    );
                    GET DIAGNOSTICS moved = ROW_COUNT;
                    total := total + moved;
                    EXIT WHEN moved = 0;
                END LOOP;
                RAISE NOTICE '0065 remap %: % rows moved to survivor', '{table}', total;
            END $$;
            """
        )

    # ── 3. Quarantine non-survivor orgs (audit trail, invisible) ───────
    op.execute(
        f"""
        DO $$
        DECLARE
            quarantined INT;
        BEGIN
            UPDATE organizations
            SET is_active = FALSE,
                status = 'rejected',
                name = '[QUARANTINED] ' || name
            WHERE id <> '{PLATFORM_ORG_ID}' AND name NOT LIKE '[QUARANTINED] %';
            GET DIAGNOSTICS quarantined = ROW_COUNT;
            RAISE NOTICE '0065 quarantine: % non-survivor orgs quarantined', quarantined;
        END $$;
        """
    )

    # ── 4. Backfill registration_mode + replace the CHECK ───────────────
    # ``instance_settings`` is created by 0066 (this migration predates the
    # table) — the ``to_regclass`` guard is intentionally a no-op on fresh
    # installs, where 0066 seeds the row with the new CHECK directly.
    # Legacy rows (pre-0066 databases carrying removed values) are
    # backfilled here wherever the table exists.
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.instance_settings') IS NULL THEN
                RETURN;
            END IF;
            ALTER TABLE instance_settings DROP CONSTRAINT IF EXISTS ck_instance_settings_registration_mode;
            UPDATE instance_settings SET registration_mode = 'invite_only'
            WHERE registration_mode NOT IN ('open', 'invite_only', 'approval');
            ALTER TABLE instance_settings ALTER COLUMN registration_mode
            SET DEFAULT 'invite_only';
            ALTER TABLE instance_settings ADD CONSTRAINT
            ck_instance_settings_registration_mode CHECK
            (registration_mode IN ('open', 'invite_only', 'approval'));
        END $$;
        """
    )

    # ── 5. Per-user config override column ─────────────────────────────
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS config_override "
        "JSONB NOT NULL DEFAULT '{}'"
    )


def downgrade() -> None:
    """Partial rollback — schema only.

    The FK remap (step 2) and the survivor election (step 1) are
    intentionally irreversible: pre-merge org attribution is not
    recoverable.  This downgrade only drops ``users.config_override``
    and restores the legacy ``registration_mode`` CHECK/default.
    """
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS config_override")
    # Mirror of step 4: no-op where ``instance_settings`` was never created.
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.instance_settings') IS NULL THEN
                RETURN;
            END IF;
            ALTER TABLE instance_settings DROP CONSTRAINT IF EXISTS ck_instance_settings_registration_mode;
            UPDATE instance_settings SET registration_mode =
            'enabled_self_serve_org_creation'
            WHERE registration_mode NOT IN
            ('disabled', 'enabled_with_org_code',
             'enabled_self_serve_org_creation');
            ALTER TABLE instance_settings ALTER COLUMN registration_mode
            SET DEFAULT 'enabled_self_serve_org_creation';
            ALTER TABLE instance_settings ADD CONSTRAINT
            ck_instance_settings_registration_mode CHECK
            (registration_mode IN ('disabled', 'enabled_with_org_code',
             'enabled_self_serve_org_creation'));
        END $$;
        """
    )
