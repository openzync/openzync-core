"""Create prompt_templates table.

Revision ID: 0012
Revises: 4d5e6f7a8b9c
Create Date: 2026-06-14
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: str | None = "4d5e6f7a8b9c"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("template_name", sa.VARCHAR(100), nullable=False),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("description", sa.VARCHAR(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
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
        sa.UniqueConstraint(
            "organization_id",
            "template_name",
            "version",
            name="uq_prompt_templates_org_name_version",
        ),
    )
    op.create_index(
        "ix_prompt_templates_org_name_active",
        "prompt_templates",
        ["organization_id", "template_name"],
        postgresql_where=sa.text("is_active = true"),
    )

    # Seed system-default prompt templates from .jinja2 files
    prompts_dir = Path(__file__).resolve().parent.parent.parent / "services" / "worker" / "prompts"
    if prompts_dir.is_dir():
        for f in sorted(prompts_dir.glob("*.jinja2")):
            template_name = f.stem
            template_text = f.read_text()
            op.execute(
                sa.text("""
                    INSERT INTO prompt_templates (organization_id, template_name, template_text, version, is_active)
                    VALUES (NULL, :name, :text, 1, true)
                    ON CONFLICT DO NOTHING
                """).bindparams(name=template_name, text=template_text)
            )


def downgrade() -> None:
    op.drop_index("ix_prompt_templates_org_name_active", table_name="prompt_templates")
    op.drop_table("prompt_templates")
