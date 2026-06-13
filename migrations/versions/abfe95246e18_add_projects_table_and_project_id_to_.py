"""Add projects table and project_id to sessions/graph_entities/graph_relationships.

Introduces a ``projects`` layer between Organization and Sessions/Graph.
Every existing organization gets a single "Default Project" and all of its
existing sessions / graph entities / graph relationships are backfilled
to reference it.

Steps:
1.  CREATE TABLE projects
2.  CREATE TABLE project_members
3.  Add ``project_id`` to sessions (nullable)
4.  Add ``project_id`` to graph_entities (nullable)
5.  Add ``project_id`` to graph_relationships (nullable)
6.  For each existing org: create a Default Project, add all users as
    members (role=admin), backfill project_id across all three tables.
7.  ALTER COLUMN project_id SET NOT NULL on all three tables.
8.  Add FK constraints, indexes.

Revision ID: abfe95246e18
Revises: 2a1b3c4d5e6f
Create Date: 2026-06-13 18:54:57.290692
"""

from __future__ import annotations

from typing import ClassVar

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "abfe95246e18"
down_revision: str | None = "2a1b3c4d5e6f"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    connection = op.get_bind()

    # ── 1. Create projects table ──────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_project_org_name"),
    )
    op.create_index(
        "ix_projects_organization_id",
        "projects",
        ["organization_id"],
    )

    # ── 2. Create project_members table ───────────────────────────────────
    op.create_table(
        "project_members",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=50), server_default=sa.text("'member'"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_member"),
        sa.CheckConstraint(
            "role IN ('admin', 'member', 'viewer')",
            name="ck_project_member_role",
        ),
    )
    op.create_index("ix_project_members_project_id", "project_members", ["project_id"])
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])

    # ── 3. Add project_id to sessions (nullable initially) ────────────────
    op.add_column(
        "sessions",
        sa.Column("project_id", sa.Uuid(), nullable=True),
    )

    # ── 4. Add project_id to graph_entities (nullable initially) ──────────
    op.add_column(
        "graph_entities",
        sa.Column("project_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_graph_entities_project_id",
        "graph_entities",
        ["project_id"],
    )

    # ── 5. Add project_id to graph_relationships (nullable initially) ─────
    op.add_column(
        "graph_relationships",
        sa.Column("project_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_graph_relationships_project_id",
        "graph_relationships",
        ["project_id"],
    )

    # ── 6. Backfill: one Default Project per org ──────────────────────────
    if connection.engine.dialect.has_table(connection, "organizations"):
        rows = connection.execute(
            sa.text("SELECT id FROM organizations WHERE is_active = true")
        ).fetchall()

        for (org_id,) in rows:
            # 5a. Create Default Project
            result = connection.execute(
                sa.text(
                    """INSERT INTO projects (organization_id, name, description)
                       VALUES (:org_id, :name, :desc)
                       RETURNING id"""
                ),
                {"org_id": org_id, "name": "Default", "desc": "Auto-created default project"},
            )
            default_project_id = result.scalar_one()

            # 5b. Add all users as admin members of the default project
            connection.execute(
                sa.text(
                    """INSERT INTO project_members (project_id, user_id, role)
                       SELECT :project_id, id, 'admin'
                       FROM users
                       WHERE organization_id = :org_id
                         AND is_deleted = false"""
                ),
                {"project_id": default_project_id, "org_id": org_id},
            )

            # 5c. Backfill sessions
            connection.execute(
                sa.text(
                    """UPDATE sessions
                       SET project_id = :project_id
                       WHERE organization_id = :org_id
                         AND project_id IS NULL"""
                ),
                {"project_id": default_project_id, "org_id": org_id},
            )

            # 5d. Backfill graph_entities
            connection.execute(
                sa.text(
                    """UPDATE graph_entities
                       SET project_id = :project_id
                       WHERE organization_id = :org_id
                         AND project_id IS NULL"""
                ),
                {"project_id": default_project_id, "org_id": org_id},
            )

            # 5e. Backfill graph_relationships
            connection.execute(
                sa.text(
                    """UPDATE graph_relationships
                       SET project_id = :project_id
                       WHERE organization_id = :org_id
                         AND project_id IS NULL"""
                ),
                {"project_id": default_project_id, "org_id": org_id},
            )

    # ── 6. Make project_id NOT NULL ───────────────────────────────────────
    op.alter_column("sessions", "project_id", nullable=False)
    op.alter_column("graph_entities", "project_id", nullable=False)
    op.alter_column("graph_relationships", "project_id", nullable=False)

    # ── 7. Add FK constraints and indexes for sessions.project_id ─────────
    op.create_foreign_key(
        "fk_sessions_project_id",
        "sessions",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_sessions_project_id",
        "sessions",
        ["project_id"],
    )

    # ── 8. Add FK constraint for graph_entities.project_id ────────────────
    op.create_foreign_key(
        "fk_graph_entities_project_id",
        "graph_entities",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # ── 9. Add FK constraint for graph_relationships.project_id ───────────
    op.create_foreign_key(
        "fk_graph_relationships_project_id",
        "graph_relationships",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """Reverse the migration — drop project_id columns and tables."""
    # Drop FK constraints
    op.drop_constraint("fk_sessions_project_id", "sessions", type_="foreignkey")
    op.drop_constraint("fk_graph_entities_project_id", "graph_entities", type_="foreignkey")
    op.drop_constraint("fk_graph_relationships_project_id", "graph_relationships", type_="foreignkey")

    # Drop indexes
    op.drop_index("ix_sessions_project_id", table_name="sessions")
    op.drop_index("ix_graph_entities_project_id", table_name="graph_entities")
    op.drop_index("ix_graph_relationships_project_id", table_name="graph_relationships")

    # Drop columns
    op.drop_column("sessions", "project_id")
    op.drop_column("graph_entities", "project_id")
    op.drop_column("graph_relationships", "project_id")

    # Drop tables (project_members first due to FK)
    op.drop_table("project_members")
    op.drop_table("projects")
