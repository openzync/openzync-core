"""Add composite indexes for server-side sorting on list endpoints.

Every sortable ``sort_by`` key gets a leading-scope composite btree index
so ORDER BY + keyset/cursor pagination stays index-only. No table or
column changes — index-only migration, fully reversible.

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES: list[str] = [
    # ── users (GET /v1/users; default created_at/desc) ──────────────
    "CREATE INDEX IF NOT EXISTS ix_users_org_created "
    "ON users (organization_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_users_org_external "
    "ON users (organization_id, external_id)",
    "CREATE INDEX IF NOT EXISTS ix_users_org_name ON users (organization_id, name)",
    "CREATE INDEX IF NOT EXISTS ix_users_org_email ON users (organization_id, email)",
    # ── sessions (default created_at/desc) ───────────────────────────
    "CREATE INDEX IF NOT EXISTS ix_sessions_project_created "
    "ON sessions (project_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_project_external "
    "ON sessions (project_id, external_id)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_project_updated "
    "ON sessions (project_id, updated_at DESC)",
    # ── episodes / messages (sequence locked; created_at alt) ────────
    "CREATE INDEX IF NOT EXISTS ix_episodes_session_created "
    "ON episodes (session_id, created_at)",
    # ── facts: project listing (default valid_from/desc) ─────────────
    "CREATE INDEX IF NOT EXISTS ix_facts_project_valid_from "
    "ON facts (project_id, valid_from DESC NULLS LAST)",
    "CREATE INDEX IF NOT EXISTS ix_facts_project_created "
    "ON facts (project_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_facts_project_confidence "
    "ON facts (project_id, confidence DESC)",
    "CREATE INDEX IF NOT EXISTS ix_facts_project_subject "
    "ON facts (project_id, subject)",
    # ── facts: session listing (default created_at/desc) ─────────────
    "CREATE INDEX IF NOT EXISTS ix_facts_org_created "
    "ON facts (organization_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_facts_org_confidence "
    "ON facts (organization_id, confidence DESC)",
    "CREATE INDEX IF NOT EXISTS ix_facts_org_subject "
    "ON facts (organization_id, subject)",
    # ── fact history (default at_time/desc) ──────────────────────────
    "CREATE INDEX IF NOT EXISTS ix_fact_invalidation_events_new_at_time "
    "ON fact_invalidation_events (new_fact_id, at_time DESC)",
    # ── projects (default created_at/desc) ───────────────────────────
    "CREATE INDEX IF NOT EXISTS ix_projects_org_created "
    "ON projects (organization_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_projects_org_name "
    "ON projects (organization_id, name)",
    "CREATE INDEX IF NOT EXISTS ix_projects_org_updated "
    "ON projects (organization_id, updated_at DESC)",
    # ── project members (default created_at/asc) ─────────────────────
    "CREATE INDEX IF NOT EXISTS ix_project_members_project_created "
    "ON project_members (project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_project_members_project_role "
    "ON project_members (project_id, role)",
    # ── audit logs (default created_at/desc) ─────────────────────────
    "CREATE INDEX IF NOT EXISTS ix_audit_logs_org_action "
    "ON audit_logs (organization_id, action)",
    "CREATE INDEX IF NOT EXISTS ix_audit_logs_org_actor "
    "ON audit_logs (organization_id, actor_id)",
    "CREATE INDEX IF NOT EXISTS ix_audit_logs_org_status_code "
    "ON audit_logs (organization_id, ((details ->> 'status_code')))",
    # ── api keys (default created_at/desc) ───────────────────────────
    "CREATE INDEX IF NOT EXISTS ix_api_keys_org_project_created "
    "ON api_keys (organization_id, project_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_api_keys_org_project_name "
    "ON api_keys (organization_id, project_id, name)",
    "CREATE INDEX IF NOT EXISTS ix_api_keys_org_project_last_used "
    "ON api_keys (organization_id, project_id, last_used_at DESC)",
    # ── webhooks (default created_at/desc) ───────────────────────────
    "CREATE INDEX IF NOT EXISTS ix_webhook_endpoints_org_created "
    "ON webhook_endpoints (organization_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_webhook_endpoints_org_name "
    "ON webhook_endpoints (organization_id, name)",
    # ── extraction schemas (default created_at/desc) ─────────────────
    "CREATE INDEX IF NOT EXISTS ix_extraction_schemas_org_created "
    "ON extraction_schemas (organization_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_extraction_schemas_org_name "
    "ON extraction_schemas (organization_id, name)",
    "CREATE INDEX IF NOT EXISTS ix_extraction_schemas_org_type "
    "ON extraction_schemas (organization_id, type)",
    # ── prompt templates (default name/asc in grouped view) ──────────
    "CREATE INDEX IF NOT EXISTS ix_prompt_templates_org_name "
    "ON prompt_templates (organization_id, template_name)",
    "CREATE INDEX IF NOT EXISTS ix_prompt_templates_org_type "
    "ON prompt_templates (organization_id, type)",
    # ── organizations (default created_at/desc) ──────────────────────
    "CREATE INDEX IF NOT EXISTS ix_organizations_created "
    "ON organizations (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_organizations_name ON organizations (name)",
    # ── graph entities (default created_at/asc) ──────────────────────
    "CREATE INDEX IF NOT EXISTS ix_graph_entities_project_created "
    "ON graph_entities (organization_id, project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_graph_entities_project_name "
    "ON graph_entities (organization_id, project_id, name)",
    "CREATE INDEX IF NOT EXISTS ix_graph_entities_project_type "
    "ON graph_entities (organization_id, project_id, entity_type)",
    # ── graph relationships (default created_at/desc) ────────────────
    "CREATE INDEX IF NOT EXISTS ix_graph_rels_project_created "
    "ON graph_relationships (organization_id, project_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_graph_rels_project_type "
    "ON graph_relationships (organization_id, project_id, relationship_type)",
    # ── graph observations (default backend-specific; sort keys) ─────
    "CREATE INDEX IF NOT EXISTS ix_observations_project_created "
    "ON graph_observations (organization_id, project_id, created_at)",
]


def upgrade() -> None:
    """Create all sortable-column composite indexes."""
    for stmt in INDEXES:
        op.execute(stmt)


def downgrade() -> None:
    """Drop all indexes created in :func:`upgrade` (reverse order)."""
    for stmt in reversed(INDEXES):
        # "CREATE INDEX IF NOT EXISTS <name> ON ..." → "DROP INDEX IF EXISTS <name>"
        name = stmt.split("CREATE INDEX IF NOT EXISTS ", 1)[1].split(" ", 1)[0]
        op.execute(f"DROP INDEX IF EXISTS {name}")
