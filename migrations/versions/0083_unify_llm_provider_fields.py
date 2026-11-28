"""Unify LLM provider fields — backend-scoped credential carryover.

Stored per-user configs used provider-specific credential keys
(``openai_api_key``, ``anthropic_api_key``, ``azure_openai_endpoint``,
``azure_openai_key``, ``ollama_base_url``, ``openai_like_base_url``)
plus provider-specific model keys (``openai_model``, ``anthropic_model``).
The unified shape keeps ``llm_backend`` / ``llm_model`` /
``llm_temperature`` / ``llm_max_tokens`` and stores credentials as
``llm_api_key`` (openai/anthropic/openai_like/Azure key),
``llm_base_url`` (ollama/openai_like), ``azure_endpoint`` and
``azure_deployment``.

Carries forward **only** the credential matching each row's
``llm_backend`` (backend-scoped, ``llm_backend`` is authoritative):

* ``openai`` → ``openai_api_key`` ⇒ ``llm_api_key``
* ``anthropic`` → ``anthropic_api_key`` ⇒ ``llm_api_key``
* ``azure`` → ``azure_openai_endpoint``/``azure_openai_key`` ⇒
  ``azure_endpoint``/``llm_api_key`` (plus ``azure_deployment``, already
  unified — carried 1:1 when present under that name)
* ``ollama`` → ``ollama_base_url`` ⇒ ``llm_base_url``
* ``openai_like`` → ``openai_like_base_url`` ⇒ ``llm_base_url`` plus the
  shared ``openai_api_key`` ⇒ ``llm_api_key``

Carryover never overwrites: a unified key is written only when the target
is absent on that row.  ``llm_model`` is kept untouched.  All eight legacy
LLM keys are then deleted from every row that still carries them; the
``WHERE`` guard plus the delete-if-present ``-`` operator make re-runs a
no-op.  ``NULL``/non-object ``config_override`` rows are skipped
(NULL-safe).

Raw SQL equivalent (per row, ``cfg`` = ``users.config_override``)::

    carry =
        (backend = 'openai'     AND cfg ? 'openai_api_key'
         AND NOT cfg ? 'llm_api_key' → {'llm_api_key': cfg->'openai_api_key'})
      | (backend = 'anthropic'  AND cfg ? 'anthropic_api_key'
         AND NOT cfg ? 'llm_api_key' → {'llm_api_key': cfg->'anthropic_api_key'})
      | (backend = 'azure'      AND cfg ? 'azure_openai_endpoint'
         AND NOT cfg ? 'azure_endpoint' → {'azure_endpoint': ...})
      | (backend = 'azure'      AND cfg ? 'azure_openai_key'
         AND NOT cfg ? 'llm_api_key' → {'llm_api_key': ...})
      | (backend = 'ollama'     AND cfg ? 'ollama_base_url'
         AND NOT cfg ? 'llm_base_url' → {'llm_base_url': ...})
      | (backend = 'openai_like' AND cfg ? 'openai_like_base_url'
         AND NOT cfg ? 'llm_base_url' → {'llm_base_url': ...})
      | (backend = 'openai_like' AND cfg ? 'openai_api_key'
         AND NOT cfg ? 'llm_api_key' → {'llm_api_key': ...})
    SET config_override = (cfg || carry)
        - 'openai_api_key' - 'anthropic_api_key'
        - 'azure_openai_endpoint' - 'azure_openai_key'
        - 'ollama_base_url' - 'openai_like_base_url'
        - 'openai_model' - 'anthropic_model'

Revision ID: 0083
Revises: 0082

Rollback note: downgrade is a no-op — carried unified values are kept and
removed legacy values are not restored.  Re-apply via
``PATCH /admin/platform-config`` if needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_KEYS = (
    "'openai_api_key' - 'anthropic_api_key' "
    "- 'azure_openai_endpoint' - 'azure_openai_key' "
    "- 'ollama_base_url' - 'openai_like_base_url' "
    "- 'openai_model' - 'anthropic_model'"
)


def upgrade() -> None:
    """Carry backend-matching credentials to unified keys, drop legacy keys."""
    # RLS is FORCE + fail-closed (0059/0060): DML must bypass it, same
    # one-liner as 0044/0050/0053/0065/0073/0074/0075/0076/0077. Session-local;
    # dies with the connection.
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")
    op.execute(  # noqa: S608 — static migration SQL
        "UPDATE users SET config_override = (COALESCE(config_override, '{}') "
        "|| CASE WHEN config_override->>'llm_backend' = 'openai' "
        "AND config_override ? 'openai_api_key' "
        "AND NOT (config_override ? 'llm_api_key') "
        "THEN jsonb_build_object('llm_api_key', config_override->'openai_api_key') "
        "ELSE '{}' END "
        "|| CASE WHEN config_override->>'llm_backend' = 'anthropic' "
        "AND config_override ? 'anthropic_api_key' "
        "AND NOT (config_override ? 'llm_api_key') "
        "THEN jsonb_build_object('llm_api_key', config_override->'anthropic_api_key') "
        "ELSE '{}' END "
        "|| CASE WHEN config_override->>'llm_backend' = 'azure' "
        "AND config_override ? 'azure_openai_endpoint' "
        "AND NOT (config_override ? 'azure_endpoint') "
        "THEN jsonb_build_object('azure_endpoint', config_override->'azure_openai_endpoint') "
        "ELSE '{}' END "
        "|| CASE WHEN config_override->>'llm_backend' = 'azure' "
        "AND config_override ? 'azure_openai_key' "
        "AND NOT (config_override ? 'llm_api_key') "
        "THEN jsonb_build_object('llm_api_key', config_override->'azure_openai_key') "
        "ELSE '{}' END "
        "|| CASE WHEN config_override->>'llm_backend' = 'ollama' "
        "AND config_override ? 'ollama_base_url' "
        "AND NOT (config_override ? 'llm_base_url') "
        "THEN jsonb_build_object('llm_base_url', config_override->'ollama_base_url') "
        "ELSE '{}' END "
        "|| CASE WHEN config_override->>'llm_backend' = 'openai_like' "
        "AND config_override ? 'openai_like_base_url' "
        "AND NOT (config_override ? 'llm_base_url') "
        "THEN jsonb_build_object('llm_base_url', config_override->'openai_like_base_url') "
        "ELSE '{}' END "
        "|| CASE WHEN config_override->>'llm_backend' = 'openai_like' "
        "AND config_override ? 'openai_api_key' "
        "AND NOT (config_override ? 'llm_api_key') "
        "THEN jsonb_build_object('llm_api_key', config_override->'openai_api_key') "
        "ELSE '{}' END) "
        f"- {_LEGACY_KEYS} "
        "WHERE config_override ?| ARRAY['openai_api_key', 'anthropic_api_key', "
        "'azure_openai_endpoint', 'azure_openai_key', 'ollama_base_url', "
        "'openai_like_base_url', 'openai_model', 'anthropic_model']"
    )


def downgrade() -> None:
    """No-op — carried unified values are kept, legacy keys not restored."""
