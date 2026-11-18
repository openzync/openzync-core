"""Drop ``organizations.plan`` — the subscription tier never gated behavior.

The ``plan`` column (``free``/``pro``/``enterprise``) was write-only
metadata: no router, service, quota check, or RLS policy ever read it.
Keeping a billing-looking column that gates nothing invites exactly the
wrong assumption (that upgrading ``plan`` unlocks something), so it is
removed instead of maintained.

- The plan CHECK constraint is dropped first — probing both historical
  names (``0001`` created ``ck_organizations_plan`` plural; the model
  later referenced ``ck_organization_plan`` singular), since either may
  own the live constraint depending on the database's migration
  history.
- Stale ``POST /admin/organizations`` callers that still send ``plan``
  are unaffected: Pydantic ignores the extra field and the endpoint
  still returns 201.

Revision ID: 0063
Revises: 0062
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the plan CHECK (either historical name) and the column."""
    # IF EXISTS probes pg_constraint — exactly one of these names owns
    # the live constraint, depending on the database's history.
    op.execute(
        "ALTER TABLE organizations "
        "DROP CONSTRAINT IF EXISTS ck_organizations_plan"
    )
    op.execute(
        "ALTER TABLE organizations "
        "DROP CONSTRAINT IF EXISTS ck_organization_plan"
    )
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS plan")


def downgrade() -> None:
    """No-op: dropped plan values are unrecoverable by design (see docstring)."""
