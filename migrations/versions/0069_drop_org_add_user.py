"""Drop org scoping — every tenant row becomes user-owned.

Destructive, no backfill: all rows in the 18 org-scoped tables (every
``ORG_SCOPED_TABLE`` except ``users``) are wiped, ``organization_id`` is
dropped, and ``user_id UUID NOT NULL REFERENCES users(id) ON DELETE
CASCADE`` is added (the creator owns their rows). ``users`` keeps its
``id`` PK and only loses its own ``organization_id`` column.

RLS: every ``org_*`` policy is dropped and replaced with a ``user_*``
policy ``USING (user_id = current_setting('app.user_id')::uuid)``
(plus ``WITH CHECK`` on the same expression), ``FORCE ROW LEVEL
SECURITY`` is (re-)applied. The ``organizations`` table is left inert —
it is dropped in 0070 after this proves out in dev.

Rollback note: irreversible by design (rows are wiped, org attribution
is not recoverable). The downgrade re-adds an ``organization_id`` column
and restores the org policies structurally, but cannot restore data.

Revision ID: 0069
Revises: 0068
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror of 0065 ORG_SCOPED_TABLES minus ``users`` (uses its ``id`` PK),
# plus ``graph_relationships`` (carries its own ``organization_id`` but was
# missed by 0065's list — its policy reads the column directly).
# ``organizations`` itself is intentionally absent — dropped in 0070.
USER_TABLES: tuple[str, ...] = (
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
    "graph_relationships",
    "custom_instructions",
    "extraction_schemas",
    "dialog_classifications",
    "structured_extractions",
    "audit_logs",
    "refresh_tokens",
    "llm_usage",
)

# Tables that ALREADY carry a ``user_id`` owner column alongside the
# legacy ``organization_id`` scope (pins, sessions, episodes, facts,
# refresh tokens): only the org column is dropped; the existing
# ``user_id`` becomes the RLS owner (FK added where missing).
ALREADY_USER_TABLES: tuple[str, ...] = (
    "project_pins",
    "sessions",
    "episodes",
    "facts",
    "refresh_tokens",
)

# Of those, tables whose ``user_id`` lacks an FK to users(id).
NEEDS_USER_FK: tuple[str, ...] = (
    "facts",
    "refresh_tokens",
)

# Satellite tables with NO own organization_id, scoped via a join to
# projects: their org_* policy must be rewritten to the user join.
SATELLITE_TABLES: tuple[str, ...] = (
    "project_members",
    "graph_episode_entities",
)

_BYPASS = "(current_setting('app.bypass_rls', true) = 'true')"
_OWNER = "user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"
_ME = "id = NULLIF(current_setting('app.user_id', true), '')::uuid"


def _using(expr: str) -> str:
    """OR the RLS bypass into a USING/WITH CHECK expression."""
    return f"({_BYPASS} OR ({expr}))"


def upgrade() -> None:
    """Wipe tenant rows, swap organization_id → user_id, rebuild RLS."""
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")

    tables = ", ".join(f"'{t}'" for t in (*USER_TABLES, *SATELLITE_TABLES, "users"))
    # ── 1. Drop all org_* RLS policies (they reference the column) ────
    op.execute(
        f"""
        DO $$
        DECLARE
            r RECORD;
        BEGIN
            FOR r IN
                SELECT pol.polname, tbl.relname AS tablename
                FROM pg_policy pol
                JOIN pg_class tbl ON tbl.oid = pol.polrelid
                JOIN pg_namespace nsp ON nsp.oid = tbl.relnamespace
                WHERE nsp.nspname = 'public'
                  AND tbl.relname IN ({tables})
                  AND pol.polname LIKE 'org\\_%'
            LOOP
                EXECUTE format(
                    'DROP POLICY IF EXISTS %I ON %I', r.polname, r.tablename
                );
            END LOOP;
        END $$;
        """
    )
    # ── 2. Drop every FK pointing at organizations (incl. users.org) ──
    op.execute(
        f"""
        DO $$
        DECLARE
            r RECORD;
        BEGIN
            FOR r IN
                SELECT tbl.relname AS tablename, con.conname
                FROM pg_constraint con
                JOIN pg_class tbl ON tbl.oid = con.conrelid
                JOIN pg_class ref ON ref.oid = con.confrelid
                JOIN pg_namespace nsp ON nsp.oid = tbl.relnamespace
                WHERE con.contype = 'f'
                  AND ref.relname = 'organizations'
                  AND nsp.nspname = 'public'
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I DROP CONSTRAINT %I',
                    r.tablename, r.conname
                );
            END LOOP;
        END $$;
        """
    )
    # ── 3. Destructive wipe (no backfill) so user_id can be NOT NULL ───
    # DEV-ONLY, never prod: every tenant row in USER_TABLES is wiped.
    # Safe here because the user model is brand-new (no prod data yet);
    # do NOT copy this pattern to a migration that runs against prod data.
    op.execute(f"TRUNCATE {', '.join(USER_TABLES)} CASCADE")
    # ── 3b. Drop UNIQUE/CHECK constraints + indexes on organization_id ──
    # (multi-column UNIQUEs like uq_projects_org_name block DROP COLUMN;
    # plain indexes are dropped explicitly so no stray org-named index
    # survives on the new user_id column).
    for table in (*USER_TABLES, "users"):
        op.execute(
            f"""
            DO $$
            DECLARE
                r RECORD;
            BEGIN
                FOR r IN
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = '{table}'::regclass
                      AND contype IN ('u', 'c', 'x')
                      AND pg_get_constraintdef(oid) LIKE '%organization_id%'
                LOOP
                    EXECUTE format(
                        'ALTER TABLE {table} DROP CONSTRAINT %I', r.conname
                    );
                END LOOP;
                FOR r IN
                    SELECT indexrelid::regclass::text AS idx
                    FROM pg_index
                    WHERE indrelid = '{table}'::regclass
                      AND NOT indisprimary
                      AND pg_get_indexdef(indexrelid) LIKE '%organization_id%'
                      AND NOT EXISTS (
                          SELECT 1 FROM pg_constraint
                          WHERE conindid = indexrelid
                      )
                LOOP
                    EXECUTE format('DROP INDEX %s', r.idx);
                END LOOP;
            END $$;
            """
        )
    # ── 4. Swap the columns ────────────────────────────────────────────
    # ``audit_logs`` stays NULL-able (logged-out writes); every other table is NOT NULL.
    # ``audit_logs`` is the one exception to NOT NULL: it was nullable
    # before (unauthenticated actions log with no user) and stays NULL.
    # Its WITH CHECK additionally permits NULL so logged-out writes succeed;
    # the USING clause still hides NULL rows from every tenant (fail-closed).
    for table in USER_TABLES:
        if table in ALREADY_USER_TABLES:
            # Owner column already exists — drop the org scope, point the
            # owner at users(id), rebuild the policy on the existing column.
            op.execute(
                f"ALTER TABLE {table} DROP COLUMN IF EXISTS organization_id"
            )
            if table == "refresh_tokens":
                # Legacy Text column holding UUID strings.  The USING cast
                # is safe ONLY because step 3 truncated the table — on a
                # non-empty table a non-UUID string would abort the ALTER.
                op.execute(
                    "ALTER TABLE refresh_tokens ALTER COLUMN user_id "
                    "TYPE UUID USING user_id::uuid"
                )
            if table in NEEDS_USER_FK:
                op.execute(
                    f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "
                    f"{table}_user_id_fkey"
                )
                op.execute(
                    f"ALTER TABLE {table} ADD CONSTRAINT {table}_user_id_fkey "
                    f"FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
                )
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table}")
            op.execute(
                f"CREATE POLICY user_isolation ON {table} "
                f"USING ({_using(_OWNER)}) "
                f"WITH CHECK ({_using(_OWNER)})"
            )
            continue
        null = "NULL" if table == "audit_logs" else "NOT NULL"
        check = (
            "(user_id IS NULL OR user_id = "
            "NULLIF(current_setting('app.user_id', true), '')::uuid)"
            if table == "audit_logs"
            else "user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"
        )
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS organization_id")
        op.execute(f"ALTER TABLE {table} ADD COLUMN user_id UUID {null}")
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "
            f"{table}_user_id_fkey"
        )
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_user_id_fkey "
            f"FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
        )
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table}")
        op.execute(
            f"CREATE POLICY user_isolation ON {table} "
            f"USING ({_using(_OWNER)}) "
            f"WITH CHECK ({_using(check)})"
        )
    # ── 5. Rewrite satellite policies (scoped via projects join) ───
    for table in SATELLITE_TABLES:
        op.execute(f"DROP POLICY IF EXISTS org_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table}")
        op.execute(
            f"CREATE POLICY user_isolation ON {table} "
            f"USING ({_using('project_id IN (SELECT id FROM projects WHERE ' + _OWNER + ')')}) "
            f"WITH CHECK ({_using('project_id IN (SELECT id FROM projects WHERE ' + _OWNER + ')')})"
        )
    # ``users`` loses its org column; its ``id`` is the ownership root.
    # Signup is public, so INSERT stays open (unique constraints dedup) —
    # reads/writes are self-only.
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS organization_id")
    # The old composite (organization_id, external_id) unique died with the
    # column (step 3b) — external_id is now globally unique.
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS uq_user_external")
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT uq_user_external UNIQUE (external_id)"
    )
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS org_isolation ON users")
    op.execute("DROP POLICY IF EXISTS org_join_code_lookup ON users")
    op.execute("DROP POLICY IF EXISTS user_isolation ON users")
    # Pending-only hard-delete guard (app-level, see
    # UserRepository.hard_delete_pending_user): a DELETE of a users row is
    # only a revoke/self-clean when ``invite_token_hash IS NOT NULL`` —
    # accepted or ordinary users must never be hard-deleted.  The policy
    # below admits any self-row; the pending-only restriction lives in the
    # repository predicate, not in RLS.
    op.execute(
        "CREATE POLICY user_isolation_select ON users FOR SELECT "
        f"USING ({_using(_ME)})"
    )
    op.execute(
        "CREATE POLICY user_isolation_write ON users FOR UPDATE "
        f"USING ({_using(_ME)})"
    )
    op.execute(
        "CREATE POLICY user_isolation_delete ON users FOR DELETE "
        f"USING ({_using(_ME)})"
    )
    op.execute("CREATE POLICY user_isolation_insert ON users FOR INSERT WITH CHECK (true)")
    # NOTE (0070): DROP TABLE organizations (left inert here) after dev proves out.


def downgrade() -> None:
    """Structural rollback only — wiped data is not recoverable."""
    for table in SATELLITE_TABLES:
        op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table}")
    for table in USER_TABLES:
        op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table}")
        if table in ALREADY_USER_TABLES:
            # Reverse what the upgrade added to the pre-existing column.
            if table in NEEDS_USER_FK:
                op.execute(
                    f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "
                    f"{table}_user_id_fkey"
                )
            if table == "refresh_tokens":
                op.execute(
                    "ALTER TABLE refresh_tokens ALTER COLUMN user_id TYPE TEXT "
                    "USING user_id::text"
                )
            continue
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_user_id_fkey"
        )
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS user_id")
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN organization_id UUID "
            f"REFERENCES organizations(id)"
        )
    for table in SATELLITE_TABLES:
        op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table}")
    op.execute("DROP POLICY IF EXISTS user_isolation_select ON users")
    op.execute("DROP POLICY IF EXISTS user_isolation_write ON users")
    op.execute("DROP POLICY IF EXISTS user_isolation_delete ON users")
    op.execute("DROP POLICY IF EXISTS user_isolation_insert ON users")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS uq_user_external")
    op.execute(
        "ALTER TABLE users ADD COLUMN organization_id UUID "
        "REFERENCES organizations(id)"
    )
