"""Backfill ``platform:manage`` onto root + admins (data-only, idempotent).

``platform:manage`` gates the platform-wide admin reads (audit log,
user listing, sitewide stats).  Rows that predate the permission would
otherwise 403 on those endpoints despite being admins: union the grant
into ``users.permissions`` wherever it is missing.

``users.permissions`` is ``ARRAY(String)`` (see 0050) — the union is a
deduped ``array_agg`` over ``COALESCE(permissions, '{}')``, and the
``NOT ... @>`` guard makes re-runs a no-op.  Follows the 0080/0081
``op.execute`` raw-SQL style (no ORM in migrations).

Raw SQL equivalent::

    UPDATE users
    SET permissions = (
        SELECT array_agg(DISTINCT p ORDER BY p)
        FROM unnest(COALESCE(users.permissions, '{}')
                || ARRAY['platform:manage']::character varying[]) AS p
    )
    WHERE (external_id = 'root' OR role = 'admin')
      AND NOT (COALESCE(permissions, '{}') @> ARRAY['platform:manage']::character varying[]);

Revision ID: 0082
Revises: 0081

Rollback note: downgrade is a deliberate no-op — removing the grant on
rollback could lock admins out of the platform reads the permission
gates.  Revoke explicitly per-user if needed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Union ``platform:manage`` into root/admin permission arrays."""
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("users")}
    if "permissions" not in cols:
        return
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")
    op.execute(
        "UPDATE users SET permissions = ("  # noqa: S608 — static migration SQL
        "SELECT array_agg(DISTINCT p ORDER BY p) "
        "FROM unnest(COALESCE(users.permissions, '{}') "
        "|| ARRAY['platform:manage']::character varying[]) AS p) "
        "WHERE (external_id = 'root' OR role = 'admin') "
        "AND NOT (COALESCE(permissions, '{}') @> ARRAY['platform:manage']::character varying[])"
    )


def downgrade() -> None:
    """No-op (see rollback note above)."""
