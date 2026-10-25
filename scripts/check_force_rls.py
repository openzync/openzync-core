#!/usr/bin/env python3
"""List tables with ``organization_id`` missing FORCE row-level security.

Runtime/CI assertion for the 0059/0060 invariant: PostgreSQL exempts the
table *owner* from non-forced RLS policies, and the application role owns
every table — so a tenant table without ``FORCE ROW LEVEL SECURITY`` is
a silent cross-tenant read hole.

Usage:
    OZ_DATABASE_URL=postgresql://... python scripts/check_force_rls.py

Exit 0 when every ``organization_id`` table is forced (and has a
policy); exit 1 listing offenders otherwise.
"""

from __future__ import annotations

import asyncio
import os
import sys

_CATALOG_SQL = """
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
"""


async def main() -> int:
    """Run the catalog check; return the process exit code."""
    try:
        import asyncpg
    except ImportError:
        print("check_force_rls: asyncpg is not installed", file=sys.stderr)
        return 2

    dsn = os.environ.get("OZ_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print(
            "check_force_rls: set OZ_DATABASE_URL (or DATABASE_URL)",
            file=sys.stderr,
        )
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(_CATALOG_SQL)
    finally:
        await conn.close()

    offenders = [
        f"{r['table_name']} (policy={r['has_policy']}, "
        f"rls={r['rls_enabled']}, force={r['rls_forced']})"
        for r in rows
        if not r["has_policy"] or not r["rls_forced"]
    ]
    if offenders:
        print("Tables missing FORCE RLS or a policy:")
        for line in offenders:
            print(f"  - {line}")
        return 1
    print(f"OK: {len(rows)} organization_id tables carry FORCE RLS.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
