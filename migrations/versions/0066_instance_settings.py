"""Create instance_settings table (missed in 0029).

Revision ID: 0066
Revises: 0065

The model (models/instance_settings.py) and repository
(repositories/instance_settings_repository.py) predate this table — 0065
step 4 guards on ``to_regclass(...) IS NULL`` precisely because no
migration ever created it, so ``GET /v1/auth/registration-status``,
``POST /v1/auth/signup`` and ``POST /v1/auth/join`` 500 with
UndefinedTable on fresh installs. Single-row table, fixed ``id = 1``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``instance_settings`` and seed the singleton row."""
    op.create_table(
        "instance_settings",
        # NOTE: Integer (not UUID) — matches the model and the repository's
        # fixed ``_INSTANCE_SETTINGS_PK = 1`` single-row idiom.
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "registration_mode",
            sa.String(64),
            nullable=False,
            server_default="invite_only",
        ),
        sa.Column(
            "initialized",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column("bootstrap_token_hash", sa.Text(), nullable=True),
        sa.Column(
            "default_backends",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "registration_mode IN ('open', 'invite_only', 'approval')",
            name="ck_instance_settings_registration_mode",
        ),
        comment="Platform-wide singleton settings (exactly one row, id=1)",
    )
    op.execute(
        "INSERT INTO instance_settings (id) VALUES (1) "
        "ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    """Drop the ``instance_settings`` table."""
    op.drop_table("instance_settings")
