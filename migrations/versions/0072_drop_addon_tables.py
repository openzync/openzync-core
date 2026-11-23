"""Drop the addon platform tables (0072 removes the addon feature).

Drops ``addon_states`` (created by 0064) plus every per-addon table the
addon alembic runner created: ``addon_alembic_<slug>`` version tables
and ``addon_<slug>_<table>`` data tables.  Slugs are unknown at revision
time, so the upgrade enumerates ``information_schema`` by prefix instead
of naming tables.  0064 is kept for chain integrity; rolling back past it
recreates an empty ``addon_states``.

Revision ID: 0072
Revises: 0071

Rollback note: run ``alembic downgrade -1`` with the same
``OZ_DATABASE_URL`` used for the upgrade.  Dropped per-addon data is not
restored — reinstall from the addon source.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _addon_tables() -> list[str]:
    """List live ``addon_*`` tables (excluding ``addon_states``).

    Returns:
        Table names starting with ``addon_`` other than ``addon_states``.
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return sorted(
        t
        for t in insp.get_table_names()
        if t.startswith("addon_") and t != "addon_states"
    )


def upgrade() -> None:
    """Drop per-addon tables, then the ``addon_states`` registry."""
    for table in _addon_tables():
        op.drop_table(table)
    op.drop_table("addon_states", if_exists=True)


def downgrade() -> None:
    """Recreate the empty ``addon_states`` registry table.

    Rollback note: per-addon ``addon_alembic_*`` / ``addon_*`` data tables
    are not restored by this downgrade.
    """
    op.create_table(
        "addon_states",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("installed_sha", sa.Text(), nullable=True),
        sa.Column("latest_seen", sa.Text(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "update_available",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "uid",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.func.gen_random_uuid(),
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
        comment="Platform-global addon install registry (platform-wide enablement)",
    )
