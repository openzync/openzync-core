"""Add indexes for audit_logs table.

The audit_logs table will carry every HTTP request for every user,
so queries by org + time range or by action need proper indexes.

Revision ID: 2a1b3c4d5e6f
Revises: 226db3adb6ba
Create Date: 2026-06-13
"""

from __future__ import annotations

from typing import ClassVar

from alembic import op
import sqlalchemy as sa

revision: str = "2a1b3c4d5e6f"
down_revision: str | None = "226db3adb6ba"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Composite index for the primary query pattern: "show me my org's logs, newest first"
    op.create_index(
        "idx_audit_logs_org_created",
        "audit_logs",
        ["organization_id", sa.text("created_at DESC")],
        postgresql_using="btree",
    )
    # Index for filtering by action type
    op.create_index(
        "idx_audit_logs_action",
        "audit_logs",
        ["action"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index("idx_audit_logs_action")
    op.drop_index("idx_audit_logs_org_created")
