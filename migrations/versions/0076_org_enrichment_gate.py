"""Move the enrichment gate to org-level: strip root gate, normalize org gates.

The gate block moves from ``schemas.system_config`` to
``schemas.organization_config`` as ``enrichment_gate{enabled, threshold}``
with the model fixed to ``OpenZync/enrichment-gating-v1``:

1. Platform root row (``external_id = 'root'``): delete the system
   ``enrichment_gate`` block entirely — ``GET /admin/system/config``
   returns ``{}`` now.
2. Org/user rows: legacy boolean gates (``true``/``false``) become
   ``{"enabled": <bool>, "threshold": 0.5}`` objects.
3. Org/user rows: strip the nested ``enrichment_gate -> model`` subkey
   from object gates (fixed default, never configured).

Revision ID: 0076
Revises: 0075

Rollback note: downgrade is a no-op — removed values are not restored.
Re-apply the gate at org level if needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Strip the root gate block; normalize org gates to model-less objects."""
    # RLS is FORCE + fail-closed (0059/0060): DML must bypass it, same
    # one-liner as 0044/0050/0053/0065/0073/0074/0075. Session-local; dies with the connection.
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")
    # 1. Root row: the system gate block is gone (org-level only now).
    op.execute(
        "UPDATE users SET config_override = config_override - 'enrichment_gate' "
        "WHERE external_id = 'root'"
    )
    # 2. Org rows: legacy booleans become {enabled, threshold} objects.
    op.execute(
        "UPDATE users SET config_override = jsonb_set("
        "config_override, '{enrichment_gate}', "
        "jsonb_build_object('enabled', "
        "(config_override -> 'enrichment_gate')::boolean, 'threshold', 0.5), "
        "true) "
        "WHERE external_id != 'root' "
        "AND jsonb_typeof(config_override -> 'enrichment_gate') = 'boolean'"
    )
    # 3. Org rows: drop the nested model subkey (fixed default, not configurable).
    op.execute(
        "UPDATE users SET config_override = "
        "config_override #- '{enrichment_gate,model}' "
        "WHERE external_id != 'root' "
        "AND jsonb_typeof(config_override -> 'enrichment_gate') = 'object'"
    )


def downgrade() -> None:
    """No-op — removed gate values are not restored."""
