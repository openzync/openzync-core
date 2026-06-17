"""Remove system-level prompt template rows.

After Option A, system defaults are sourced from ``manifest.yaml`` +
``.jinja2`` files on disk.  System-level rows (``organization_id IS NULL``)
are no longer needed and are removed.

The existing indexes (``ix_prompt_templates_org_name_active``,
``uq_prompt_templates_org_name_version``,
``uq_prompt_templates_default_type``) remain unchanged — they work correctly
with org-scoped rows only.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-17
"""

from __future__ import annotations

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Delete all system-level prompt template rows.

    After this migration, all ``prompt_templates`` rows have a non-NULL
    ``organization_id``.  Orgs that already had seeded copies keep them
    unchanged — only the system-level originals are removed.
    """
    op.execute(
        sa.text("DELETE FROM prompt_templates WHERE organization_id IS NULL")
    )


def downgrade() -> None:
    """Re-seed system-level prompt templates from disk.

    Reads the ``.jinja2`` files from ``services/worker/prompts/`` and
    re-inserts them as system-level rows (``organization_id IS NULL``),
    matching the original migration ``0012`` behaviour.

    Note:
        If the prompt files have changed between the upgrade and downgrade,
        the re-inserted rows may differ from the originals.  This is
        intentional — the disk is the canonical source.
    """
    prompts_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "services" / "worker" / "prompts"
    )
    if not prompts_dir.is_dir():
        return

    for f in sorted(prompts_dir.glob("*.jinja2")):
        template_name = f.stem
        if template_name == "manifest":
            continue  # skip the manifest file
        template_text = f.read_text()
        op.execute(
            sa.text("""
                INSERT INTO prompt_templates
                    (organization_id, template_name, template_text, version, is_active)
                VALUES (NULL, :name, :text, 1, true)
                ON CONFLICT DO NOTHING
            """).bindparams(name=template_name, text=template_text)
        )
