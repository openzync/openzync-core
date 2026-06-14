"""Create webhook_delivery_logs table.

Revision ID: 4d5e6f7a8b9c
Revises: 3f8a1b2c4d5e
Create Date: 2026-06-14
"""

from __future__ import annotations

from typing import ClassVar

from alembic import op
import sqlalchemy as sa

revision: str = "4d5e6f7a8b9c"
down_revision: str | None = "3f8a1b2c4d5e"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_delivery_logs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error", sa.Text(), nullable=True),
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
            ["endpoint_id"],
            ["webhook_endpoints.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_webhook_delivery_logs_endpoint",
        "webhook_delivery_logs",
        ["endpoint_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_delivery_logs_endpoint")
    op.drop_table("webhook_delivery_logs")
