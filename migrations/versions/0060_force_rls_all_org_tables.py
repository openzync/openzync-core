"""Enforce FORCE row-level security on every table carrying organization_id.

Post-0059 drift guard: any tenant table created without ``FORCE ROW LEVEL
SECURITY`` (or without an org-isolation policy at all) is a silent
cross-tenant read hole, because PostgreSQL exempts the table owner from
non-forced policies and the application role owns every table.

The upgrade scans the live catalog for tables with an ``organization_id``
column and:

* raises loudly listing any table with **no** policy — the operator must
  add an ``org_isolation_*`` policy first (forcing RLS without a policy
  would deny all rows, fail-closed but breaking);
* applies ``FORCE ROW LEVEL SECURITY`` to every remaining table missing
  the flag (idempotent — already-forced tables are untouched).

Rollback note: the downgrade is a documented no-op. Removing the FORCE
flag would re-open the exact hole 0059 closed; no data is changed by
keeping it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CATALOG_SQL = text("""
SELECT c.relname AS table_name,
       c.relrowsecurity AS rls_enabled,
       c.relforcerowsecurity AS rls_forced,
       EXISTS (
           SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid
       ) AS has_policy
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p')
  AND EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = 'public'
        AND table_name = c.relname
        AND column_name = 'organization_id'
  )
ORDER BY 1
""")


def upgrade() -> None:
    """FORCE RLS on every ``organization_id`` table; fail loud on policy gaps."""
    conn = op.get_bind()
    rows = conn.execute(_CATALOG_SQL).all()

    policyless = [r.table_name for r in rows if not r.has_policy]
    if policyless:
        raise RuntimeError(
            "Refusing to enforce RLS: tables carry organization_id but have "
            f"no policy (add org_isolation_* first): {', '.join(policyless)}"
        )

    for row in rows:
        if not row.rls_forced:
            op.execute(f'ALTER TABLE "{row.table_name}" FORCE ROW LEVEL SECURITY')


def downgrade() -> None:
    """No-op: removing FORCE would re-open the cross-tenant read hole."""
