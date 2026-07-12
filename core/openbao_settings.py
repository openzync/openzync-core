"""Async settings loader that reads runtime configuration from OpenBao.

The **only** bootstrap path is :func:`init_settings` — it reads system secrets
from the OpenBao KV store and populates the :class:`core.config.Settings`
singleton.  There is no env-var fallback.

Usage::

    from core.config import BootstrapSettings
    from core.openbao import OpenBaoClient
    from core.openbao_settings import init_settings

    bootstrap = BootstrapSettings()
    async with OpenBaoClient(
        bootstrap.OPENBAO_ADDR,
        bootstrap.OPENBAO_ROLE_ID,
        bootstrap.OPENBAO_SECRET_ID,
    ) as bao:
        settings = await init_settings(bao)
        # settings.DATABASE_URL, settings.SECRET_KEY, …
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.openbao import SYSTEM_KEY_MAPPING, OpenBaoClient

if TYPE_CHECKING:
    from core.config import Settings

# ═══════════════════════════════════════════════════════════════════════════════
# Fields that require integer casting
# ═══════════════════════════════════════════════════════════════════════════════

_INT_FIELDS = {
    "MAX_WORKERS",
    "JWT_ACCESS_TOKEN_TTL_MINUTES",
    "JWT_REFRESH_TOKEN_TTL_DAYS",
    "FALKORDB_MAX_CONNECTIONS",
    "FALKORDB_SOCKET_TIMEOUT",
    "RATE_LIMIT_IP_MAX",
    "RATE_LIMIT_WINDOW_SEC",
    "SMTP_PORT",
}
"""Set of ``Settings`` field names whose values must be cast to ``int``.

OpenBao stores all values as strings in its KV store.  These fields need
explicit casting from ``str`` → ``int``.
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


async def init_settings(client: OpenBaoClient) -> Settings:
    """Read system secrets from OpenBao and populate the ``Settings`` singleton.

    Uses :meth:`OpenBaoClient.read_system_config` to fetch the raw
    key/value pairs, maps the OpenBao key names to ``Settings`` field
    names via :data:`core.openbao.SYSTEM_KEY_MAPPING`, and performs
    appropriate type casting for integer fields.

    The resulting instance is stored as the module-level singleton via
    :func:`core.config.set_settings` and can be retrieved with
    :func:`core.config.get_settings`.

    Args:
        client: An authenticated :class:`OpenBaoClient` instance.

    Returns:
        A fully-populated :class:`Settings` instance.

    Raises:
        OpenBaoAuthError: If the client token is invalid.
        OpenBaoConnectionError: If OpenBao is unreachable.
    """
    # Late import to avoid circular dependency at module level.
    from core.config import Settings, set_settings  # noqa: PLC0415

    raw: dict[str, Any] = await client.read_system_config()

    kwargs: dict[str, Any] = {}
    for bao_key, env_key in SYSTEM_KEY_MAPPING.items():
        if bao_key not in raw:
            continue
        # Convert e.g. "OZ_DATABASE_URL" → "DATABASE_URL"
        field_name = env_key.removeprefix("OZ_")
        value = raw[bao_key]
        if field_name in _INT_FIELDS and value is not None:
            value = int(value)
        kwargs[field_name] = value

    settings = Settings(**kwargs)
    set_settings(settings)
    return settings
