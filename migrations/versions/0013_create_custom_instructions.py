"""Create custom_instructions table.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "custom_instructions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.VARCHAR(50), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
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

    # Unique functional index: one active instruction of a given name per
    # (org, scope, target).  COALESCE handles NULL target_id so that
    # org-level (target_id IS NULL) rows are also covered — PostgreSQL treats
    # each NULL as distinct in a plain unique index, so without COALESCE you
    # could have duplicates where target_id IS NULL.
    op.create_index(
        "ix_custom_instructions_unique_scope_name",
        "custom_instructions",
        [
            "organization_id",
            "scope",
            sa.text("COALESCE(target_id, '00000000-0000-0000-0000-000000000000')"),
            "name",
        ],
        unique=True,
    )

    # Composite B-tree index for fast lookups by (org, scope, target_id).
    # Most reads query by these three columns together.
    op.create_index(
        "ix_custom_instructions_scope_target",
        "custom_instructions",
        ["organization_id", "scope", "target_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_custom_instructions_scope_target", table_name="custom_instructions")
    op.drop_index("ix_custom_instructions_unique_scope_name", table_name="custom_instructions")
    op.drop_table("custom_instructions")
