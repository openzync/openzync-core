"""Strip all 12 removed platform-default keys from the root config override.

Platform system config keeps only ``enrichment`` + ``enrichment_gate``
(see ``schemas/system_config.py``) — the 8 ``llm_*`` / ``reranker_*``
keys (0073) plus ``graph_backend``, ``graph_search_type``,
``graph_max_traversal_depth``, ``context_cache_ttl`` are gone.
Deletes all 12 from the platform root user's ``users.config_override``
JSONB so no stale value lingers.  Scoped to
``external_id = 'root'`` — org/user rows are untouched.

Revision ID: 0074
Revises: 0073

Rollback note: downgrade is a no-op — removed values are not restored.
Re-apply them at org level if needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keys removed from SystemConfigUpdate / SystemConfigResponse (0073 + this).
_REMOVED_KEYS: tuple[str, ...] = (
    "llm_backend",
    "llm_model",
    "llm_temperature",
    "llm_max_tokens",
    "reranker_backend",
    "reranker_model",
    "reranker_top_k",
    "reranker_top_n",
    "graph_backend",
    "graph_search_type",
    "graph_max_traversal_depth",
    "context_cache_ttl",
)


def upgrade() -> None:
    """Delete the removed keys from the platform root row only."""
    # RLS is FORCE + fail-closed (0059/0060): DML must bypass it, same
    # one-liner as 0044/0050/0053/0065/0073. Session-local; dies with the connection.
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")
    removals = " - ".join(f"'{key}'" for key in _REMOVED_KEYS)
    op.execute(
        f"UPDATE users SET config_override = config_override - {removals} "
        "WHERE external_id = 'root'"
    )


def downgrade() -> None:
    """No-op — removed platform defaults are not restored."""
