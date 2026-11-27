"""Collapse roles to admin/member — two-role model with root immortality.

Role mapping (single UPDATE, old values visible to every SET expression):
- ``superadmin`` → ``admin`` (wildcard powers + RLS bypass + /admin/system).
- ``admin`` → ``member`` (demoted; permissions backfilled to the full
  vocabulary so the demotion never locks the user out).
- ``member`` → untouched.

``permissions`` arrays are always subsets of the vocabulary, so the
"empty or subset" backfill condition reduces to ``role = 'admin'`` —
every ex-admin row receives the full sorted vocabulary.

Constraint ``ck_users_role`` shrinks to ``('admin', 'member')``.  Root
immortality (``external_id = 'root'``) is enforced in the service layer
(``services/user_service.py``), not by CHECK — no DDL needed for it.

Raw SQL equivalent::

    UPDATE users
    SET role = CASE
            WHEN role = 'superadmin' THEN 'admin'
            WHEN role = 'admin' THEN 'member'
            ELSE role
        END,
        permissions = CASE
            WHEN role = 'admin' THEN
                '{configuration:read,configuration:write,members:read,
                  members:write,project:manage,project:read,project:write}'
            ELSE permissions
        END
    WHERE role IN ('superadmin', 'admin');
    ALTER TABLE users DROP CONSTRAINT ck_users_role;
    ALTER TABLE users ADD CONSTRAINT ck_users_role
        CHECK (role IN ('admin', 'member'));

Revision ID: 0080
Revises: 0079

Rollback note (lossy): the downgrade only re-expands the CHECK to admit
``superadmin`` again — it does NOT restore which rows were ``superadmin``
(the mapping is not invertible).  A demoted ex-admin stays ``member``
with full permissions (still valid under the old constraint).  To
re-establish a platform admin after a rollback, use a direct DB/data
fix — the promotion endpoint stays frozen and cannot promote.
Run ``alembic downgrade -1`` with the same ``OZ_DATABASE_URL`` used for the
upgrade.
"""

from __future__ import annotations

from alembic import op

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | None = None
depends_on: str | None = None

_ALL_PERMISSIONS_LITERAL = (
    "{configuration:read,configuration:write,members:read,"
    "members:write,project:manage,project:read,project:write}"
)
"""Full vocabulary as a Postgres array literal (sorted, canonical order)."""


def upgrade() -> None:
    """Collapse ``superadmin``→``admin``, ``admin``→``member`` + shrink CHECK."""
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")

    op.execute(
        f"UPDATE users SET role = CASE "  # noqa: S608 — static migration SQL
        "WHEN role = 'superadmin' THEN 'admin' "
        "WHEN role = 'admin' THEN 'member' "
        "ELSE role END, "
        f"permissions = CASE WHEN role = 'admin' THEN '{_ALL_PERMISSIONS_LITERAL}' "
        "ELSE permissions END "
        "WHERE role IN ('superadmin', 'admin')"
    )

    op.drop_constraint("ck_users_role", "users", type_="check")
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IN ('admin', 'member')",
    )


def downgrade() -> None:
    """Re-expand the role CHECK (data mapping is NOT reversed — see note)."""
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IN ('admin', 'member', 'superadmin')",
    )
