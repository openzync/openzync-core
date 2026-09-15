"""Add project_pins table.

Per-user dashboard pins: a user may pin at most 3 projects per
organization (limit enforced in the service layer). Pins are a
preference, not membership — CASCADE on user/project/org delete.

Revision ID: 0051
Revises: 0050
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_pins",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "pinned_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "project_id", name="uq_project_pins_user_project"
        ),
    )
    op.create_index(
        "ix_project_pins_user_org_pinned",
        "project_pins",
        ["user_id", "organization_id", "pinned_at"],
    )
    op.create_index("ix_project_pins_project_id", "project_pins", ["project_id"])

    op.execute("ALTER TABLE project_pins ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation_project_pins ON project_pins
        FOR ALL
        USING (
            current_setting('app.bypass_rls', true) = 'true'
            OR organization_id = current_setting('app.org_id')::UUID
        )
    """)


def downgrade() -> None:
    # Rollback note: pinned state is a reconstructible UI preference —
    # downgrading drops all pins with no data-recovery path. Re-upgrade
    # starts from an empty pin set.
    op.drop_table("project_pins")
