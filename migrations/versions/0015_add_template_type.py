"""Add type and is_default_for_type columns to prompt_templates.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "prompt_templates",
        sa.Column("type", sa.String(50), nullable=True),
    )
    op.add_column(
        "prompt_templates",
        sa.Column(
            "is_default_for_type",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )

    # Backfill: map existing template names to their types
    backfill_mapping = {
        "fact_extraction": (
            "extract_facts_v3",
            "extract_facts_v4",
        ),
        "entity_extraction": (
            "extract_entities_v3",
            "extract_entities_v4",
        ),
        "classification": ("classify_dialog_v1",),
        "structured_extraction": ("extract_structured_v1",),
        "user_summary": ("summarise_user_v1",),
    }
    for type_name, names in backfill_mapping.items():
        placeholders = ", ".join(f":n{i}" for i in range(len(names)))
        params = {f"n{i}": n for i, n in enumerate(names)}
        op.execute(
            sa.text(
                f"UPDATE prompt_templates SET type = :type, "
                f"is_default_for_type = true "
                f"WHERE template_name IN ({placeholders})"
            ).bindparams(type=type_name, **params)
        )

    # Dedup: the backfill may have set is_default_for_type = true on both
    # system-default and org-specific copies.  Keep only the highest-version
    # row per (organization_id, type) as the active default.
    op.execute(
        sa.text("""
            UPDATE prompt_templates pt
            SET is_default_for_type = false
            WHERE pt.type IS NOT NULL
              AND pt.id NOT IN (
                SELECT DISTINCT ON (organization_id, type) id
                FROM prompt_templates
                WHERE type IS NOT NULL
                ORDER BY organization_id, type, version DESC
            )
        """)
    )

    # Only one active default per type per scope
    op.create_index(
        "uq_prompt_templates_default_type",
        "prompt_templates",
        ["organization_id", "type"],
        postgresql_where=sa.text("is_default_for_type = true"),
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_prompt_templates_default_type", table_name="prompt_templates")
    op.drop_column("prompt_templates", "is_default_for_type")
    op.drop_column("prompt_templates", "type")
