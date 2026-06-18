"""Add project_id to all entity tables, backfill data, and update constraints.

Adds project_id (FK -> projects) to every entity table for project-scoped
isolation. Backfills existing rows by creating a personal project per user
and assigning all of that user's existing data to it.

Drops the old sessions (user_id, external_id) unique constraint and replaces
it with (project_id, external_id) to match the new ownership model.

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-18
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # ═════════════════════════════════════════════════════════════════════
    # STEP 1: Add project_id columns (nullable initially)
    # ═════════════════════════════════════════════════════════════════════

    # 1a. sessions — NOT NULL after backfill
    op.add_column("sessions", sa.Column("project_id", sa.Uuid(), nullable=True))

    # 1b. episodes — NOT NULL after backfill
    op.add_column("episodes", sa.Column("project_id", sa.Uuid(), nullable=True))

    # 1c. facts — NOT NULL after backfill
    op.add_column("facts", sa.Column("project_id", sa.Uuid(), nullable=True))

    # 1d. graph_entities — NOT NULL after backfill
    op.add_column("graph_entities", sa.Column("project_id", sa.Uuid(), nullable=True))

    # 1e. graph_relationships — NOT NULL after backfill
    op.add_column("graph_relationships", sa.Column("project_id", sa.Uuid(), nullable=True))

    # 1f. graph_episode_entities — NOT NULL after backfill
    op.add_column("graph_episode_entities", sa.Column("project_id", sa.Uuid(), nullable=True))

    # 1g. structured_extractions — NOT NULL after backfill
    op.add_column("structured_extractions", sa.Column("project_id", sa.Uuid(), nullable=True))

    # 1h. dialog_classifications — NOT NULL after backfill
    op.add_column("dialog_classifications", sa.Column("project_id", sa.Uuid(), nullable=True))

    # 1i. api_keys — nullable (backward compatible; NULL = org-wide access)
    op.add_column("api_keys", sa.Column("project_id", sa.Uuid(), nullable=True))

    # ═════════════════════════════════════════════════════════════════════
    # STEP 2: Create personal projects for every existing user
    # ═════════════════════════════════════════════════════════════════════
    # We reuse the user's UUID as the project UUID so the backfill becomes a
    # simple SET project_id = user_id on sessions.  This avoids a complex
    # lookup/join during migration.
    #
    # On conflict (same org + same name) we silently skip — the existing
    # project already covers this user.

    op.execute("""
        INSERT INTO projects (id, organization_id, name, created_by)
        SELECT
            u.id,
            u.organization_id,
            COALESCE(NULLIF(u.name, ''), 'User') || '''s Project',
            u.id
        FROM users u
        ON CONFLICT (organization_id, name) DO NOTHING
    """)

    # For any users whose project creation was skipped due to a name conflict,
    # create a disambiguated project using their external_id or UUID.
    op.execute("""
        INSERT INTO projects (id, organization_id, name, created_by)
        SELECT
            u.id,
            u.organization_id,
            COALESCE(NULLIF(u.name, ''), 'User') || '''s Project (' || u.external_id || ')',
            u.id
        FROM users u
        WHERE NOT EXISTS (
            SELECT 1 FROM projects p
            WHERE p.id = u.id
        )
        ON CONFLICT (organization_id, name) DO NOTHING
    """)

    # Final fallback — use user UUID in the name (guaranteed unique)
    op.execute("""
        INSERT INTO projects (id, organization_id, name, created_by)
        SELECT
            u.id,
            u.organization_id,
            'Project (' || u.id::text || ')',
            u.id
        FROM users u
        WHERE NOT EXISTS (
            SELECT 1 FROM projects p
            WHERE p.id = u.id
        )
    """)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 3: Add each user as the owner of their personal project
    # ═════════════════════════════════════════════════════════════════════

    op.execute("""
        INSERT INTO project_members (project_id, user_id, role)
        SELECT p.id, u.id, 'owner'
        FROM users u
        INNER JOIN projects p ON p.id = u.id
        ON CONFLICT (project_id, user_id) DO NOTHING
    """)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 4: Backfill project_id on sessions
    # ═════════════════════════════════════════════════════════════════════
    # Since we used the user UUID as project UUID, project_id = user_id.
    op.execute("""
        UPDATE sessions
        SET project_id = user_id
        WHERE project_id IS NULL
    """)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 5: Backfill project_id on episodes (derived from session)
    # ═════════════════════════════════════════════════════════════════════
    op.execute("""
        UPDATE episodes
        SET project_id = s.project_id
        FROM sessions s
        WHERE episodes.session_id = s.id
          AND episodes.project_id IS NULL
    """)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 6: Backfill project_id on facts (derived from source episode)
    # ═════════════════════════════════════════════════════════════════════
    op.execute("""
        UPDATE facts
        SET project_id = e.project_id
        FROM episodes e
        WHERE facts.source_episode_id = e.id
          AND facts.project_id IS NULL
    """)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 7: Backfill project_id on structured_extractions (via session)
    # ═════════════════════════════════════════════════════════════════════
    op.execute("""
        UPDATE structured_extractions
        SET project_id = s.project_id
        FROM sessions s
        WHERE structured_extractions.session_id = s.id
          AND structured_extractions.project_id IS NULL
    """)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 8: Backfill project_id on dialog_classifications (via episode)
    # ═════════════════════════════════════════════════════════════════════
    op.execute("""
        UPDATE dialog_classifications
        SET project_id = e.project_id
        FROM episodes e
        WHERE dialog_classifications.episode_id = e.id
          AND dialog_classifications.project_id IS NULL
    """)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 9: Backfill project_id on graph_entities (derived from
    #          graph_episode_entities → episode → session, or from
    #          graph_relationships → episode → session.
    #          For entities without any link to an episode, fall back to
    #          the org-level project creation logic: use the org's first
    #          user's project.
    # ═════════════════════════════════════════════════════════════════════

    # First pass: from graph_episode_entities → episode → session → project
    op.execute("""
        UPDATE graph_entities ge
        SET project_id = gee.project_id
        FROM (
            SELECT DISTINCT gee.entity_id, e.project_id
            FROM graph_episode_entities gee
            INNER JOIN episodes e ON e.id = gee.episode_id
            WHERE e.project_id IS NOT NULL
        ) gee
        WHERE ge.id = gee.entity_id
          AND ge.project_id IS NULL
    """)

    # Second pass: from graph_relationships → source_episode_id → episode
    op.execute("""
        UPDATE graph_entities ge
        SET project_id = rel.project_id
        FROM (
            SELECT DISTINCT gr.source_id AS entity_id, e.project_id
            FROM graph_relationships gr
            INNER JOIN episodes e ON e.id = gr.source_episode_id
            WHERE e.project_id IS NOT NULL
        ) rel
        WHERE ge.id = rel.entity_id
          AND ge.project_id IS NULL
    """)

    op.execute("""
        UPDATE graph_entities ge
        SET project_id = rel.project_id
        FROM (
            SELECT DISTINCT gr.target_id AS entity_id, e.project_id
            FROM graph_relationships gr
            INNER JOIN episodes e ON e.id = gr.source_episode_id
            WHERE e.project_id IS NOT NULL
        ) rel
        WHERE ge.id = rel.entity_id
          AND ge.project_id IS NULL
    """)

    # Third pass: entities with no episode link — assign to a project from
    # the same org (pick the first project we can find for that org).
    # ⚠️ This is a best-effort fallback; such entities should be rare.
    op.execute("""
        UPDATE graph_entities ge
        SET project_id = sub.first_project_id
        FROM (
            SELECT ge2.id AS entity_id,
                   (SELECT p.id
                    FROM projects p
                    WHERE p.organization_id = ge2.organization_id
                    ORDER BY p.created_at ASC
                    LIMIT 1
                   ) AS first_project_id
            FROM graph_entities ge2
            WHERE ge2.project_id IS NULL
        ) sub
        WHERE ge.id = sub.entity_id
    """)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 10: Backfill project_id on graph_relationships
    # ═════════════════════════════════════════════════════════════════════
    # First pass: from source_episode_id → episode → project
    op.execute("""
        UPDATE graph_relationships gr
        SET project_id = e.project_id
        FROM episodes e
        WHERE gr.source_episode_id = e.id
          AND gr.project_id IS NULL
    """)

    # Second pass: remaining relationships with no episode link —
    # derive from source entity's project
    op.execute("""
        UPDATE graph_relationships gr
        SET project_id = ge.project_id
        FROM graph_entities ge
        WHERE gr.source_id = ge.id
          AND gr.project_id IS NULL
    """)

    # Third pass: derive from target entity's project
    op.execute("""
        UPDATE graph_relationships gr
        SET project_id = ge.project_id
        FROM graph_entities ge
        WHERE gr.target_id = ge.id
          AND gr.project_id IS NULL
    """)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 11: Backfill project_id on graph_episode_entities
    # ═════════════════════════════════════════════════════════════════════
    op.execute("""
        UPDATE graph_episode_entities gee
        SET project_id = e.project_id
        FROM episodes e
        WHERE gee.episode_id = e.id
          AND gee.project_id IS NULL
    """)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 12: Add NOT NULL constraints (except api_keys)
    # ═════════════════════════════════════════════════════════════════════
    op.alter_column("sessions", "project_id", nullable=False)
    op.alter_column("episodes", "project_id", nullable=False)
    op.alter_column("facts", "project_id", nullable=False)
    op.alter_column("graph_entities", "project_id", nullable=False)
    op.alter_column("graph_relationships", "project_id", nullable=False)
    op.alter_column("graph_episode_entities", "project_id", nullable=False)
    op.alter_column("structured_extractions", "project_id", nullable=False)
    op.alter_column("dialog_classifications", "project_id", nullable=False)
    # api_keys.project_id stays nullable (NULL = org-wide access)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 13: Foreign key constraints
    # ═════════════════════════════════════════════════════════════════════
    op.create_foreign_key(
        "fk_sessions_project_id", "sessions", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_episodes_project_id", "episodes", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_facts_project_id", "facts", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_graph_entities_project_id", "graph_entities", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_graph_relationships_project_id", "graph_relationships", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )
    # graph_episode_entities: composite PK, FK is still important
    op.create_foreign_key(
        "fk_graph_ep_entities_project_id", "graph_episode_entities", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_structured_extractions_project_id", "structured_extractions", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_dialog_classifications_project_id", "dialog_classifications", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_api_keys_project_id", "api_keys", "projects",
        ["project_id"], ["id"], ondelete="SET NULL",
    )

    # ═════════════════════════════════════════════════════════════════════
    # STEP 14: Drop old sessions unique constraint, add new one
    # ═════════════════════════════════════════════════════════════════════
    op.drop_constraint("uq_sessions_user_external", "sessions", type_="unique")
    op.create_unique_constraint(
        "uq_sessions_project_external", "sessions",
        ["project_id", "external_id"],
    )

    # ═════════════════════════════════════════════════════════════════════
    # STEP 15: Add indexes on all new project_id columns
    # ═════════════════════════════════════════════════════════════════════
    # (sessions, episodes, facts, api_keys already have index=True set below)
    op.create_index("idx_sessions_project_id", "sessions", ["project_id"])
    op.create_index("idx_episodes_project_id", "episodes", ["project_id"])
    op.create_index("idx_facts_project_id", "facts", ["project_id"])
    op.create_index("idx_graph_entities_project_id", "graph_entities", ["project_id"])
    op.create_index("idx_graph_relationships_project_id", "graph_relationships", ["project_id"])
    op.create_index("idx_graph_ep_entities_project_id", "graph_episode_entities", ["project_id"])
    op.create_index("idx_structured_extractions_project_id", "structured_extractions", ["project_id"])
    op.create_index("idx_dialog_classifications_project_id", "dialog_classifications", ["project_id"])
    op.create_index("idx_api_keys_project_id", "api_keys", ["project_id"])

    # ═════════════════════════════════════════════════════════════════════
    # STEP 16: Update RLS policies for existing tables to include project_id
    #          (RLS remains at the org level — project scoping is enforced
    #           at the application layer.  No RLS policy changes needed.)
    # ═════════════════════════════════════════════════════════════════════

    # Extend RLS to graph_entities (created in 0006 without RLS)
    op.execute("ALTER TABLE graph_entities ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation_graph_entities ON graph_entities
        FOR ALL
        USING (
            current_setting('app.bypass_rls', true) = 'true'
            OR organization_id = current_setting('app.org_id')::UUID
        )
    """)

    # Extend RLS to graph_relationships (created in 0006 without RLS)
    op.execute("ALTER TABLE graph_relationships ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation_graph_relationships ON graph_relationships
        FOR ALL
        USING (
            current_setting('app.bypass_rls', true) = 'true'
            OR organization_id = current_setting('app.org_id')::UUID
        )
    """)

    # graph_episode_entities does not have organization_id, so RLS uses
    # a subquery through episodes (or through graph_entities).
    op.execute("ALTER TABLE graph_episode_entities ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation_graph_episode_entities ON graph_episode_entities
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
    # ── Drop new foreign keys ──────────────────────────────────────────
    op.drop_constraint("fk_sessions_project_id", "sessions", type_="foreignkey")
    op.drop_constraint("fk_episodes_project_id", "episodes", type_="foreignkey")
    op.drop_constraint("fk_facts_project_id", "facts", type_="foreignkey")
    op.drop_constraint("fk_graph_entities_project_id", "graph_entities", type_="foreignkey")
    op.drop_constraint("fk_graph_relationships_project_id", "graph_relationships", type_="foreignkey")
    op.drop_constraint("fk_graph_ep_entities_project_id", "graph_episode_entities", type_="foreignkey")
    op.drop_constraint("fk_structured_extractions_project_id", "structured_extractions", type_="foreignkey")
    op.drop_constraint("fk_dialog_classifications_project_id", "dialog_classifications", type_="foreignkey")
    op.drop_constraint("fk_api_keys_project_id", "api_keys", type_="foreignkey")

    # ── Drop indexes ───────────────────────────────────────────────────
    op.drop_index("idx_sessions_project_id", table_name="sessions")
    op.drop_index("idx_episodes_project_id", table_name="episodes")
    op.drop_index("idx_facts_project_id", table_name="facts")
    op.drop_index("idx_graph_entities_project_id", table_name="graph_entities")
    op.drop_index("idx_graph_relationships_project_id", table_name="graph_relationships")
    op.drop_index("idx_graph_ep_entities_project_id", table_name="graph_episode_entities")
    op.drop_index("idx_structured_extractions_project_id", table_name="structured_extractions")
    op.drop_index("idx_dialog_classifications_project_id", table_name="dialog_classifications")
    op.drop_index("idx_api_keys_project_id", table_name="api_keys")

    # ── Restore old unique constraint on sessions ──────────────────────
    op.drop_constraint("uq_sessions_project_external", "sessions", type_="unique")
    op.create_unique_constraint(
        "uq_sessions_user_external", "sessions",
        ["user_id", "external_id"],
    )

    # ── Drop project_id columns ────────────────────────────────────────
    op.drop_column("sessions", "project_id")
    op.drop_column("episodes", "project_id")
    op.drop_column("facts", "project_id")
    op.drop_column("structured_extractions", "project_id")
    op.drop_column("dialog_classifications", "project_id")
    op.drop_column("graph_entities", "project_id")
    op.drop_column("graph_relationships", "project_id")
    op.drop_column("graph_episode_entities", "project_id")
    op.drop_column("api_keys", "project_id")

    # ── Drop RLS policies for graph tables (added in this migration) ───
    op.execute("DROP POLICY IF EXISTS org_isolation_graph_entities ON graph_entities")
    op.execute("DROP POLICY IF EXISTS org_isolation_graph_relationships ON graph_relationships")
    op.execute("DROP POLICY IF EXISTS org_isolation_graph_episode_entities ON graph_episode_entities")
