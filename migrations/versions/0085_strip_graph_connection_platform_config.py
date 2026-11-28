"""Strip per-user graph connection details — globalized to OZ_* Settings.

Per-user graph connection details were removed (clean break): FalkorDB
and SurrealDB connections are configured solely via ``OZ_FALKORDB_URL``
(required, fail-fast) and ``OZ_SURREALDB_URL`` / ``OZ_SURREALDB_USER`` /
``OZ_SURREALDB_PASS`` / ``OZ_SURREALDB_NAMESPACE`` /
``OZ_SURREALDB_DATABASE`` (optional, defaulted).  Deletes the six graph
connection keys from every stored JSONB config so no stale per-user
value (including ``surrealdb_pass`` material) lingers:

* ``users.config_override`` (authoritative store, all rows)

Only the ``graph_backend`` selector, ``graph_search_type``, and
``graph_max_traversal_depth`` stay in ``config_override``.

Raw SQL equivalent (per row, ``cfg`` = ``users.config_override``)::

    SET config_override = COALESCE(cfg, '{}')
        - 'surrealdb_url' - 'surrealdb_user' - 'surrealdb_pass'
        - 'surrealdb_namespace' - 'surrealdb_database' - 'falkordb_url'

Revision ID: 0085
Revises: 0084

Rollback note: downgrade is a no-op — removed values are not restored.
Re-apply via the env file (``OZ_FALKORDB_URL`` / ``OZ_SURREALDB_*``)
if needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0085"
down_revision: str | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Delete the six per-user graph connection keys from all stored configs."""
    # RLS is FORCE + fail-closed (0059/0060): DML must bypass it, same
    # one-liner as 0044/0050/0053/0065/0073/0074/0075/0076/0077/0083/0084.
    # Session-local; dies with the connection.
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")
    op.execute(  # noqa: S608 — static migration SQL
        "UPDATE users SET config_override = COALESCE(config_override, '{}') "
        "- 'surrealdb_url' - 'surrealdb_user' - 'surrealdb_pass' "
        "- 'surrealdb_namespace' - 'surrealdb_database' - 'falkordb_url' "
        "WHERE config_override ?| ARRAY['surrealdb_url', 'surrealdb_user', "
        "'surrealdb_pass', 'surrealdb_namespace', 'surrealdb_database', "
        "'falkordb_url']"
    )


def downgrade() -> None:
    """No-op — removed graph connection values are not restored."""
