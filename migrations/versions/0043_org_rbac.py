"""Constrain ``users.role`` to ``('admin', 'member')``.

The column already exists with ``default 'member'``; this adds a CHECK
constraint so a bad role can never be written (the RBAC layer added in
``core/rbac.py`` treats any non-``'admin'`` value as member, but the DB
should not allow the invalid state to exist at all).

Revision ID: 0043
Revises: 59c3fcb79b85
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op

revision: str = "0043"
down_revision: str | None = "59c3fcb79b85"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the ``ck_users_role`` CHECK constraint on ``users.role``."""
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IN ('admin', 'member')",
    )


def downgrade() -> None:
    """Drop the ``ck_users_role`` CHECK constraint."""
    op.drop_constraint("ck_users_role", "users", type_="check")
