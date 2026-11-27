"""Add invitee-approval status to project_members.

New memberships start as ``pending`` and become ``active`` on invitee (or
superadmin) approval. The column backfills existing rows to ``active`` via
the server default, and the default is kept so legacy inserts stay active.

Raw SQL equivalent::

    ALTER TABLE project_members ADD COLUMN status VARCHAR(20)
        DEFAULT 'active' NOT NULL;
    ALTER TABLE project_members ADD CONSTRAINT ck_project_members_status
        CHECK (status IN ('pending', 'active'));

Revision ID: 0079
Revises: 0078

Rollback note: downgrade drops the status column (pending/active state
is NOT preserved).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``status`` (backfilled to ``active``) plus its check constraint."""
    op.add_column(
        "project_members",
        sa.Column(
            "status",
            sa.String(20),
            server_default="active",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_project_members_status",
        "project_members",
        "status IN ('pending', 'active')",
    )


def downgrade() -> None:
    """Drop the check constraint and the ``status`` column."""
    op.drop_constraint(
        "ck_project_members_status", "project_members", type_="check"
    )
    op.drop_column("project_members", "status")
