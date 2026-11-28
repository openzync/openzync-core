"""Merge azure_endpoint into llm_base_url.

The Azure endpoint URL is now stored canonically in ``llm_base_url``
(``LLMConfig.base_url``; ``AzureBackend`` receives it as its ``endpoint``
constructor arg).  ``azure_endpoint`` is deleted everywhere; there is no
fallback and no compat read.

Carries ``azure_endpoint`` ⇒ ``llm_base_url`` only on rows whose
``llm_backend`` is ``'azure'`` and which do not already carry
``llm_base_url`` (never overwrite).  ``azure_endpoint`` is then deleted
from every row that still carries it; the ``WHERE`` guard plus the
delete-if-present ``-`` operator make re-runs a no-op.  ``NULL``/
non-object ``config_override`` rows are skipped (NULL-safe: ``?`` yields
NULL, not TRUE, on NULL JSONB).

Raw SQL equivalent (per row, ``cfg`` = ``users.config_override``)::

    carry = (backend = 'azure' AND cfg ? 'azure_endpoint'
             AND NOT cfg ? 'llm_base_url' → {'llm_base_url': cfg->'azure_endpoint'})
    SET config_override = (cfg || carry) - 'azure_endpoint'

Revision ID: 0084
Revises: 0083

Rollback note: downgrade is a no-op — carried ``llm_base_url`` values are
kept and removed ``azure_endpoint`` values are not restored.  Re-apply via
``PATCH /admin/platform-config`` if needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0084"
down_revision: str | None = "0083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Carry azure_endpoint to llm_base_url on azure rows, drop the key."""
    # RLS is FORCE + fail-closed (0059/0060): DML must bypass it, same
    # one-liner as 0044/0050/0053/0065/0073/0074/0075/0076/0077/0083.
    # Session-local; dies with the connection.
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")
    op.execute(  # noqa: S608 — static migration SQL
        "UPDATE users SET config_override = (COALESCE(config_override, '{}') "
        "|| CASE WHEN config_override->>'llm_backend' = 'azure' "
        "AND config_override ? 'azure_endpoint' "
        "AND NOT (config_override ? 'llm_base_url') "
        "THEN jsonb_build_object('llm_base_url', config_override->'azure_endpoint') "
        "ELSE '{}' END) "
        "- 'azure_endpoint' "
        "WHERE config_override ? 'azure_endpoint'"
    )


def downgrade() -> None:
    """No-op — carried llm_base_url values are kept, azure_endpoint not restored."""
