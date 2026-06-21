"""Add created_by column to api_keys for API-key user attribution.

Previously, API-key-authenticated requests had no user identity in
``request.state.user_id``, causing operations that require a user UUID
(e.g. session creation) to fail with 401.

This adds an optional ``created_by`` FK to ``users``, populated when an
API key is created via the dashboard JWT session. The auth middleware
reads this value and sets ``scope["state"]["user_id"]``, so
``get_current_user_id`` works for API-key requests too.

Existing keys remain ``NULL`` (no backfill).

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-21
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            comment="The user who created this API key — used for attribution "
                    "in API-key-authenticated requests.",
        ),
    )
    op.create_index(
        "ix_api_key_created_by", "api_keys", ["created_by"],
    )


def downgrade() -> None:
    op.drop_index("ix_api_key_created_by", table_name="api_keys")
    op.drop_column("api_keys", "created_by")
