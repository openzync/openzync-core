"""Strip system enrichment + gate model from the root config override.

Platform system config keeps only ``enrichment_gate`` without ``model``
(see ``schemas/system_config.py``): the ``enrichment`` block is
org-level only and the gate model is fixed to
``OpenZync/enrichment-gating-v1``.  Deletes the top-level
``enrichment`` key and the nested ``enrichment_gate -> model`` subkey
from the platform root user's ``users.config_override`` JSONB so no
stale value lingers.  Scoped to ``external_id = 'root'`` — org/user
rows are untouched.

Revision ID: 0075
Revises: 0074

Rollback note: downgrade is a no-op — removed values are not restored.
Re-apply enrichment toggles at org level if needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Delete the system enrichment block + gate model from the root row."""
    # RLS is FORCE + fail-closed (0059/0060): DML must bypass it, same
    # one-liner as 0044/0050/0053/0065/0073/0074. Session-local; dies with the connection.
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")
    op.execute(
        "UPDATE users SET config_override = "
        "(config_override - 'enrichment') #- '{enrichment_gate,model}' "
        "WHERE external_id = 'root'"
    )


def downgrade() -> None:
    """No-op — removed platform defaults are not restored."""
