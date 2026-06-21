"""Create projects and project_members tables.

Introduces the project-layer abstraction — a collaborative workspace scoped
to an organization where sessions, facts, graph knowledge, and configurations
are grouped. Each project has members with roles (owner, member).

See ADR: project-layer-addition.md

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-18
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def _rls_org_id(table: str) -> None:
    """Policy: organization_id = current org, or bypass for super-admin."""
    _enable_rls(table)
    op.execute(f"""
        CREATE POLICY org_isolation_{table} ON {table}
        FOR ALL
        USING (
            current_setting('app.bypass_rls', true) = 'true'
            OR organization_id = current_setting('app.org_id')::UUID
        )
    """)


def upgrade() -> None:
    # ── projects ────────────────────────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_projects_org_name"),
    )
    op.create_index("idx_projects_organization_id", "projects", ["organization_id"])
    op.create_index("idx_projects_created_by", "projects", ["created_by"])

    # ── project_members ─────────────────────────────────────────────────────
    op.create_table(
        "project_members",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(20), server_default=sa.text("'member'"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),
        sa.CheckConstraint("role IN ('owner', 'member')", name="ck_project_members_role"),
    )
    op.create_index("idx_project_members_project_id", "project_members", ["project_id"])
    op.create_index("idx_project_members_user_id", "project_members", ["user_id"])

    # ── RLS Policies ────────────────────────────────────────────────────────
    _rls_org_id("projects")
    # project_members does not have organization_id — RLS enforced via
    # project membership at the application layer. The org-level RLS is
    # inherited from the parent project through application-level joins.
    _enable_rls("project_members")
    op.execute("""
        CREATE POLICY org_isolation_project_members ON project_members
        FOR ALL
        USING (
            current_setting('app.bypass_rls', true) = 'true'
            OR project_id IN (
                SELECT id FROM projects
                WHERE organization_id = current_setting('app.org_id')::UUID
            )
        )
    """)


def downgrade() -> None:
    op.drop_table("project_members")
    op.drop_table("projects")
