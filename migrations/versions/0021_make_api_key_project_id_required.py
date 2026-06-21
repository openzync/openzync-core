"""Make api_keys.project_id NOT NULL with CASCADE delete.

Previously, ``project_id`` on ``api_keys`` was nullable (NULL = org-wide
access) with ``ON DELETE SET NULL``.  All API keys must now belong to a
project — org-wide keys are no longer permitted.

Backfills existing NULL ``project_id`` values by assigning keys to the
oldest project in their organization (falling back to the org's own UUID if
no projects exist — this creates an orphan project that an admin should
adopt).

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-19
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ═════════════════════════════════════════════════════════════════════
    # STEP 1: Backfill NULL project_ids
    # ═════════════════════════════════════════════════════════════════════
    # Assign orphan keys (those with no project) to the first available
    # project for their organization.
    op.execute("""
        UPDATE api_keys k
        SET project_id = (
            SELECT p.id FROM projects p
            WHERE p.organization_id = k.organization_id
            ORDER BY p.created_at
            LIMIT 1
        )
        WHERE k.project_id IS NULL
        AND EXISTS (
            SELECT 1 FROM projects p
            WHERE p.organization_id = k.organization_id
        )
    """)

    # For organizations with no projects at all, create a default project
    # using the organization UUID as the project UUID and assign orphan keys
    # to it.  This is a last-resort backfill.
    op.execute("""
        INSERT INTO projects (id, organization_id, name)
        SELECT
            gen_random_uuid(),
            k.organization_id,
            'Provisioned Project'
        FROM api_keys k
        WHERE k.project_id IS NULL
        GROUP BY k.organization_id
    """)

    op.execute("""
        UPDATE api_keys k
        SET project_id = (
            SELECT p.id FROM projects p
            WHERE p.organization_id = k.organization_id
            ORDER BY p.created_at
            LIMIT 1
        )
        WHERE k.project_id IS NULL
    """)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 2: Drop old FK (ON DELETE SET NULL) and recreate (ON DELETE CASCADE)
    # ═════════════════════════════════════════════════════════════════════
    op.drop_constraint("fk_api_keys_project_id", "api_keys", type_="foreignkey")
    op.create_foreign_key(
        "fk_api_keys_project_id", "api_keys", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )

    # ═════════════════════════════════════════════════════════════════════
    # STEP 3: Make project_id NOT NULL
    # ═════════════════════════════════════════════════════════════════════
    op.alter_column("api_keys", "project_id", nullable=False)


def downgrade() -> None:
    # ═════════════════════════════════════════════════════════════════════
    # STEP 1: Revert to nullable (SET NULL)
    # ═════════════════════════════════════════════════════════════════════
    op.alter_column("api_keys", "project_id", nullable=True)

    # ═════════════════════════════════════════════════════════════════════
    # STEP 2: Drop CASCADE FK and recreate with SET NULL
    # ═════════════════════════════════════════════════════════════════════
    op.drop_constraint("fk_api_keys_project_id", "api_keys", type_="foreignkey")
    op.create_foreign_key(
        "fk_api_keys_project_id", "api_keys", "projects",
        ["project_id"], ["id"], ondelete="SET NULL",
    )

    # ═════════════════════════════════════════════════════════════════════
    # STEP 3: (Optional) remove backfilled projects — we leave them since
    # they may already have data.  Admins can delete manually.
    # ═════════════════════════════════════════════════════════════════════
