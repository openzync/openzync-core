"""Strip llm_* / reranker_* keys from the platform root config override.

Platform system config no longer accepts or returns LLM/reranker keys
(org-level only now — see ``schemas/system_config.py``).  This revision
deletes the 8 removed keys from the platform root user's
``users.config_override`` JSONB so no stale value lingers in the
authoritative store.  Scoped to ``external_id = 'root'`` — org/user
rows are untouched.

Revision ID: 0073
Revises: 0072

Rollback note: downgrade is a no-op — removed values are not restored.
Re-apply them at org level if needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keys removed from SystemConfigUpdate / SystemConfigResponse.
_REMOVED_KEYS: tuple[str, ...] = (
    "llm_backend",
    "llm_model",
    "llm_temperature",
    "llm_max_tokens",
    "reranker_backend",
    "reranker_model",
    "reranker_top_k",
    "reranker_top_n",
)


def upgrade() -> None:
    """Delete the removed keys from the platform root row only."""
    # RLS is FORCE + fail-closed (0059/0060): DML must bypass it, same
    # one-liner as 0044/0050/0053/0065. Session-local; dies with the connection.
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")
    removals = " - ".join(f"'{key}'" for key in _REMOVED_KEYS)
    op.execute(
        f"UPDATE users SET config_override = config_override - {removals} "
        "WHERE external_id = 'root'"
    )


def downgrade() -> None:
    """No-op — removed platform defaults are not restored."""
