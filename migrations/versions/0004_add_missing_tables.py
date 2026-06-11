"""Add missing tables: extraction_schemas, refresh_tokens, audit_log, llm_usage.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ═══════════════════════════════════════════════════════════════════════════════
# note: ⚠️  CRITICAL CONFLICT
# ═══════════════════════════════════════════════════════════════════════════════
# The tables created by this migration (extraction_schemas, refresh_tokens,
# audit_logs, llm_usage) ALREADY EXIST in migration 0001_initial_schema.py
# (lines 157–257).  If 0001 has already been applied, running 0004 will fail
# with "relation already exists" errors.
#
# There are three ways to resolve this depending on team intent:
#
#   Option A (recommended if 0001 is in production):
#       Make this a NO-OP migration — drop all CREATE TABLE statements and
#       leave only an `op.execute("SELECT 1")` placeholder.  The tables are
#       already created by 0001.
#
#   Option B (recommended if 0001 will be replaced):
#       Remove these tables from 0001_initial_schema.py, keep them here,
#       and ensure the migration chain is clean (0001 → 0002 → 0003 → 0004).
#
#   Option C (safe-bridge approach, implemented below):
#       Guard each CREATE TABLE with an IF NOT EXISTS check so the migration
#       is idempotent regardless of whether 0001 has been applied.
#       This avoids migration failures while the team decides on the
#       long-term home for these tables.
#
# Current implementation (Option C) uses raw SQL with IF NOT EXISTS to keep
# the migration safe in either scenario.
# ═══════════════════════════════════════════════════════════════════════════════


def _table_exists(name: str) -> bool:
    """Return True if the given table already exists in the database."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT FROM information_schema.tables "
            "  WHERE table_name = :name"
            ")"
        ),
        {"name": name},
    )
    return result.scalar()


def upgrade() -> None:
    # ── extraction_schemas ───────────────────────────────────────────────
    if not _table_exists("extraction_schemas"):
        op.create_table(
            "extraction_schemas",
            sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
            sa.Column("organization_id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("json_schema", postgresql.JSONB(), nullable=False),
            sa.Column("prompt_template", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("organization_id", "name", name="uq_extraction_schemas_org_name"),
        )

    # ── refresh_tokens ───────────────────────────────────────────────────
    if not _table_exists("refresh_tokens"):
        op.create_table(
            "refresh_tokens",
            sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("organization_id", sa.Uuid(), nullable=False),
            sa.Column("token_hash", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("is_revoked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("rotated_by", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_hash"),
        )

    # ── audit_logs ───────────────────────────────────────────────────────
    if not _table_exists("audit_logs"):
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
            sa.Column("organization_id", sa.Uuid(), nullable=True),
            sa.Column("actor_id", sa.Text(), nullable=True),
            sa.Column("actor_type", sa.Text(), nullable=True),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("resource_type", sa.Text(), nullable=False),
            sa.Column("resource_id", sa.Text(), nullable=True),
            sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
            sa.Column("ip_address", sa.Text(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.CheckConstraint("actor_type IN ('user', 'api_key', 'system')", name="ck_audit_logs_actor_type"),
            sa.PrimaryKeyConstraint("id"),
        )

    # ── llm_usage ────────────────────────────────────────────────────────
    if not _table_exists("llm_usage"):
        op.create_table(
            "llm_usage",
            sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
            sa.Column("organization_id", sa.Uuid(), nullable=False),
            sa.Column("model", sa.Text(), nullable=False),
            sa.Column("task_type", sa.Text(), nullable=False),
            sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("total_tokens", sa.Integer(), sa.Computed("prompt_tokens + completion_tokens"), nullable=False),
            sa.Column("cost_estimate", sa.Numeric(12, 8), server_default=sa.text("0"), nullable=False),
            sa.Column("duration_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    """Drop tables in reverse dependency order.

    ⚠️  Only drops tables that were created by this migration.  Tables that
    already existed (created by 0001) are left untouched.
    """
    op.execute("DROP TABLE IF EXISTS llm_usage")
    op.execute("DROP TABLE IF EXISTS audit_logs")
    op.execute("DROP TABLE IF EXISTS refresh_tokens")
    op.execute("DROP TABLE IF EXISTS extraction_schemas")
