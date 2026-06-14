"""Create webhook_endpoints table.

Revision ID: 3f8a1b2c4d5e
Revises: 2a1b3c4d5e6f
Create Date: 2026-06-14
"""

from __future__ import annotations

from typing import ClassVar

from alembic import op
import sqlalchemy as sa

revision: str = "3f8a1b2c4d5e"
down_revision: str | None = "2a1b3c4d5e6f"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "events",
            sa.Text(),
            nullable=False,
            comment='JSON array of subscribed event types, e.g. ["session.created","fact.extracted"]',
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_webhook_endpoints_org",
        "webhook_endpoints",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_endpoints_org")
    op.drop_table("webhook_endpoints")
