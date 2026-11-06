"""Add addon settings_schema column + addon_org_settings table.

Revision ID: addon_0002
Revises: addon_0001
Create Date: 2026-11-05

Adds a nullable ``settings_schema`` JSONB snapshot column to the
platform-global ``addon_states`` (no backfill — old rows read as having no
settings), plus the org-scoped ``addon_org_settings`` table carrying
per-org enable flags and plain (non-secret) settings values.

Unlike ``addon_states``, the new table IS tenant-scoped: it carries
``organization_id`` and gets the standard ``ENABLE`` + ``FORCE ROW LEVEL
SECURITY`` treatment with the ``org_isolation_`` policy (bypass-aware,
fail-closed on unset context — same expression as migration 0059).

Rollback note: the downgrade drops the table and the column. Per-org
settings are re-creatable from the bundle schema + org input; secret
values in OpenBao are NOT touched by either direction.
"""

from __future__ import annotations

# Alembic template convention: ``Sequence`` annotates branch_labels /
# depends_on but is never a runtime value.
from collections.abc import Sequence  # noqa: TC003

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "addon_0002"
down_revision: str | None = "addon_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the settings snapshot column and the org settings table."""
    conn = op.get_bind()
    conn.execute(text("SELECT pg_advisory_xact_lock(839472, hashtext('addon_org'))"))
    op.add_column(
        "addon_states",
        sa.Column("settings_schema", postgresql.JSONB(), nullable=True),
    )
    op.create_table(
        "addon_org_settings",
        sa.Column("addon_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "values",
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
        comment="Per-org addon flags + plain settings values (RLS-scoped).",
    )
    op.execute("ALTER TABLE addon_org_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE addon_org_settings FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation_addon_org_settings ON addon_org_settings
        FOR ALL
        USING (
            current_setting('app.bypass_rls', true) = 'true'
                OR organization_id = NULLIF(
                    current_setting('app.org_id', true), '')::UUID
        )
    """)


def downgrade() -> None:
    """Drop the policy, table, and snapshot column (OpenBao untouched)."""
    op.execute(
        "DROP POLICY IF EXISTS org_isolation_addon_org_settings ON addon_org_settings"
    )
    op.drop_table("addon_org_settings")
    op.drop_column("addon_states", "settings_schema")
