"""Drop legacy ``organizations.org_code`` / ``join_enabled`` columns.

Single-root-org cleanup: self-registration join codes are gone — member
signup lands in the platform org via ``POST /v1/auth/signup`` and consults
``instance_settings.registration_mode``, never these columns.  The
``uq_organizations_org_code`` unique index goes with ``org_code``.

Raw SQL equivalent::

    ALTER TABLE organizations DROP CONSTRAINT IF EXISTS uq_organizations_org_code;
    DROP INDEX IF EXISTS uq_organizations_org_code;
    ALTER TABLE organizations DROP COLUMN org_code;
    ALTER TABLE organizations DROP COLUMN join_enabled;

Revision ID: 0081
Revises: 0080

Rollback note: downgrade re-adds both columns (``join_enabled`` NOT NULL
default ``true``; ``org_code`` nullable with the unique index restored) —
per-row join codes are NOT restored (member join is signup-only now; the
removed join endpoint cannot consume them).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the ``uq_organizations_org_code`` index and both legacy columns."""
    op.drop_index("uq_organizations_org_code", table_name="organizations")
    op.drop_column("organizations", "org_code")
    op.drop_column("organizations", "join_enabled")


def downgrade() -> None:
    """Re-add the legacy columns (data not restored)."""
    op.add_column(
        "organizations",
        sa.Column(
            "join_enabled",
            sa.BOOLEAN(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "organizations",
        sa.Column("org_code", sa.VARCHAR(24), nullable=True),
    )
    op.create_index(
        "uq_organizations_org_code",
        "organizations",
        ["org_code"],
        unique=True,
    )
