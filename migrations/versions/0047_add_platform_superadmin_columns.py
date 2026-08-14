"""Add platform super-admin columns — org status + must-change-password.

Two additions backing the platform super-admin layer:

- ``organizations.status``: lifecycle state of a tenant org —
  ``pending`` (awaiting superadmin approval), ``approved`` (live), or
  ``rejected``.  A CHECK constraint mirrors the ``ck_organization_plan``
  style.  ``server_default='approved'`` keeps every existing org live —
  the pre-feature behaviour.
- ``users.must_change_password``: forces the root/superadmin user to set
  a real password on first login (the seeded default credential must not
  persist).  Default ``false`` — ordinary users are unaffected.

Cross-feature fix: ``ck_users_role`` (added in 0043 for org RBAC) only
allowed ``('admin', 'member')`` — the platform super-admin layer needs
``users.role = 'superadmin'`` for the root user, so the constraint is
extended in-place.

Pure DDL with constant defaults — no backfill UPDATE, so no RLS session
statements are needed (unlike 0044, which had to bypass RLS for its
per-row backfill).

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add org status + must-change-password; extend the role constraint."""
    op.add_column(
        "organizations",
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'approved'"),
        ),
    )
    op.create_check_constraint(
        "ck_organization_status",
        "organizations",
        "status IN ('pending', 'approved', 'rejected')",
    )
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # The platform root user carries role='superadmin' — the 0043 tenant
    # RBAC constraint must admit it (tenant roles stay admin/member).
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IN ('admin', 'member', 'superadmin')",
    )


def downgrade() -> None:
    """Drop both columns, the status CHECK, and restore the 0043 role CHECK.

    Rollback note: run ``alembic downgrade -1`` with the same
    ``OZ_DATABASE_URL`` used for the upgrade.  A ``superadmin`` user row
    created by the platform seed blocks this downgrade until removed.
    """
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IN ('admin', 'member')",
    )
    op.drop_column("users", "must_change_password")
    op.drop_constraint("ck_organization_status", "organizations", type_="check")
    op.drop_column("organizations", "status")
