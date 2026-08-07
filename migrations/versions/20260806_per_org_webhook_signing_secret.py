"""Add per-endpoint signing secret to webhook_endpoints.

Every endpoint now carries its own HMAC-SHA256 signing secret instead of
relying on the global ``WEBHOOK_SIGNING_SECRET`` (C3 remediation — a global
secret shared by every tenant let any tenant forge signatures that all
tenants' consumers trusted).  The column is nullable so existing rows
migrate cleanly; legacy NULL rows are lazily backfilled at first emit.

Revision ID: c3a1b2f4d5e6
Revises: 14e38491d2ed
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c3a1b2f4d5e6"
down_revision: str | None = "14e38491d2ed"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the nullable per-endpoint signing secret column."""
    op.add_column(
        "webhook_endpoints",
        sa.Column("signing_secret", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop the per-endpoint signing secret column."""
    op.drop_column("webhook_endpoints", "signing_secret")
