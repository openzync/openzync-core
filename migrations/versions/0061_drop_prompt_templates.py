"""Drop the prompt_templates table — prompts are disk-only now.

The ``/admin/org/prompts*`` API is removed and per-org custom template
edits are destroyed by design: the source of truth is
``services/worker/prompts/manifest.yaml`` + the ``.jinja2`` files,
cached at startup (see ``core.prompt_manifest.DEFAULT_PROMPT_TEXTS``).

Removes the RLS policy from 0059 (``org_isolation_prompt_templates``),
the table indexes (``ix_prompt_templates_org_name_active`` from 0012,
``uq_prompt_templates_org_name_version`` from 0012,
``uq_prompt_templates_default_type`` from 0015, the two 0055 sort
indexes) and the table itself.

Rollback note: the downgrade is a documented no-op — the dropped rows
(custom org edits) cannot be reconstructed from the manifest, so
recreating an empty table would only pretend to roll back.  Restoring
requires a database backup taken before this migration.

Revision ID: 0061
Revises: 0060
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the prompt_templates policy, indexes, and table."""
    op.execute(
        "DROP POLICY IF EXISTS org_isolation_prompt_templates "
        "ON prompt_templates"
    )
    for index in (
        "ix_prompt_templates_org_name_active",
        "uq_prompt_templates_default_type",
        "ix_prompt_templates_org_name",
        "ix_prompt_templates_org_type",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index}")
    op.execute(
        "ALTER TABLE prompt_templates DROP CONSTRAINT IF EXISTS "
        "uq_prompt_templates_org_name_version"
    )
    op.drop_table("prompt_templates")


def downgrade() -> None:
    """No-op: custom template rows are unrecoverable by design (see docstring)."""
