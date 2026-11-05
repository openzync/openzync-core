"""Create addon_states — platform-scoped addon lifecycle table.

Revision ID: addon_0001
Revises: 0060
Create Date: 2026-11-05

The upgrade takes a transaction-scoped pg advisory lock so concurrent
migrators serialize on this DDL. No RLS policies: the table carries no
``organization_id`` (platform-global, superadmin-only via the bypass
session), so migration 0060's catalog scan ignores it.

Rollback note: the downgrade drops the table. Uninstall (API-level) never
drops addon data tables unless ``?purge_data=true`` — this downgrade is
the operator-level escape hatch only.
"""

from __future__ import annotations

# Alembic template convention: ``Sequence`` annotates branch_labels /
# depends_on but is never a runtime value.
from collections.abc import Sequence  # noqa: TC003

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "addon_0001"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``addon_states`` under an advisory lock."""
    conn = op.get_bind()
    conn.execute(text("SELECT pg_advisory_xact_lock(839472, hashtext('addon_states'))"))
    op.create_table(
        "addon_states",
        sa.Column("id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="installed",
        ),
        sa.Column(
            "manifest",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        comment="Platform-scoped addon install/enable state (no RLS).",
    )


def downgrade() -> None:
    """Drop ``addon_states`` (operator escape hatch — see module docstring)."""
    op.drop_table("addon_states")
