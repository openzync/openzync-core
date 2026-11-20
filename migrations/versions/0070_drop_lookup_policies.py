"""Drop dead join-code RLS policies on organizations.

``org_join_code_lookup`` (FOR SELECT, read the ``app.lookup_code`` GUC)
and ``org_anon_insert`` (FOR INSERT, join-code-era org insert) are dead
since the org-code join path was removed: no code binds ``app.lookup_code``
anymore and the join is authed now.  The ``organizations`` table itself
is left inert — its drop is deferred beyond 0070.

Revision ID: 0070
Revises: 0069
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the two dead join-code policies (idempotent)."""
    op.execute("DROP POLICY IF EXISTS org_join_code_lookup ON organizations")
    op.execute("DROP POLICY IF EXISTS org_anon_insert ON organizations")


def downgrade() -> None:
    """No-op: the dropped policies are dead and must not be resurrected."""
