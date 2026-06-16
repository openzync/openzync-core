"""Add config JSONB column to organizations and migrate llm_config data.

The ``config`` column stores all per-organization UI-exposed settings (LLM,
embeddings, graph backend, behaviour).  Fields in ``config`` supersede env-var
defaults from ``core.config.settings``.

Migration steps:
  1. Add ``config`` JSONB column with default ``{}``.
  2. Backfill ``config->'llm'`` from the deprecated ``llm_config`` column.
  3. Add a GIN index for efficient top-level key lookups.

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Step 1: Add config column
    op.add_column(
        "organizations",
        sa.Column(
            "config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Per-org UI-exposed configuration (LLM, embeddings, graph, behaviour).",
        ),
    )

    # Step 2: Backfill config->'llm' from the deprecated llm_config column
    op.execute(
        sa.text("""
            UPDATE organizations
            SET config = jsonb_set(
                config,
                '{llm}',
                COALESCE(llm_config, '{}'::jsonb)
            )
            WHERE llm_config IS NOT NULL AND llm_config != '{}'::jsonb
        """)
    )

    # Step 3: GIN index for top-level key lookups in the config JSONB column
    op.create_index(
        "ix_organizations_config_gin",
        "organizations",
        ["config"],
        postgresql_using="gin",
        postgresql_ops={"config": "jsonb_path_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_organizations_config_gin", table_name="organizations")
    op.drop_column("organizations", "config")
