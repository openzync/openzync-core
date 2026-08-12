"""Platform system settings — read-only, superadmin-only, masked list + raw reveal.

Reads the combined system secret (``system/`` namespace, ``config/data/system``)
and exposes every key in :data:`core.openbao.SYSTEM_KEY_MAPPING` under masking
rules that never leak credentials.  Mirrors ``core.system_config``'s OpenBao
error propagation — OpenBao failures are hard errors.  No caching: the list is
small, reads are cheap, and the endpoint is superadmin-only.
"""

from __future__ import annotations

import re
from typing import Any

from core.exceptions import NotFoundError
from core.openbao import SYSTEM_KEY_MAPPING, OpenBaoClient
from schemas.admin_system import (
    SYSTEM_SETTING_CATEGORIES,
    SystemSettingItem,
    SystemSettingRevealResponse,
    SystemSettingsResponse,
)

_MASK = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"
"""Mask rendered for secret values (8 bullet chars)."""

# note: explicit set rather than substring matching on PASSWORD/SECRET/KEY/TOKEN —
# "TOKEN" would false-positive on OZ_JWT_*_TOKEN_TTL_* (numeric TTLs, not secrets).
_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "OZ_SECRET_KEY",
        "OZ_WEBHOOK_SIGNING_SECRET",
        "OZ_SMTP_PASSWORD",
        "OZ_SMTP_USERNAME",  # treat SMTP username as secret — it pairs with the password
        "OZ_ROOT_PASSWORD",
    }
)

_URL_KEYS: frozenset[str] = frozenset(
    {
        "OZ_DATABASE_URL",
        "OZ_REDIS_URL",
        "OZ_FALKORDB_URL",
        "OZ_SURREALDB_URL",
    }
)

_USERINFO_RE = re.compile(r"://[^/@]+@")
"""Matches ``://<userinfo>@`` — the userinfo of a URL (never contains ``/`` or ``@``)."""


def _is_set(value: Any) -> bool:
    """True unless the raw secret value is absent or an empty string."""
    return value is not None and str(value) != ""


def _mask_url(value: str) -> str:
    """Mask a URL: keep ``scheme://host[:port]`` only when userinfo is present.

    A URL carrying credentials exposes them plus its path — both are cut.
    A URL without userinfo contains nothing sensitive and is returned
    verbatim (path included).
    """
    if _USERINFO_RE.search(value) is None:
        return value
    scheme, rest = _USERINFO_RE.sub("://", value).split("://", 1)
    return f"{scheme}://{rest.split('/', 1)[0]}"


def _mask_value(key: str, value: str) -> str:
    """Mask a single raw value per the key's type.

    Args:
        key: The ``OZ_*`` key being masked.
        value: The raw string value.

    Returns:
        The masked string: bullets for secrets, userinfo-stripped URL for
        URL keys (kept verbatim when it has no userinfo), otherwise the
        raw value.
    """
    if key in _SECRET_KEYS:
        return _MASK
    if key in _URL_KEYS:
        return _mask_url(value)
    return value


async def list_system_settings(bao_client: OpenBaoClient) -> SystemSettingsResponse:
    """List every known system setting with masked values.

    Reads the combined system secret once and returns one item per key in
    ``SYSTEM_KEY_MAPPING`` (mapping order).  Keys absent from the secret
    report ``is_set=False`` with no value.

    Args:
        bao_client: An authenticated :class:`OpenBaoClient`.

    Returns:
        A :class:`SystemSettingsResponse` with all known keys, masked.

    Raises:
        OpenBaoConnectionError: If OpenBao is unreachable.
    """
    raw = await bao_client.read_system_config()
    return SystemSettingsResponse(
        data=[
            SystemSettingItem(
                key=key,
                category=SYSTEM_SETTING_CATEGORIES[key],
                is_set=_is_set(raw.get(key)),
                masked_value=(
                    _mask_value(key, str(raw[key])) if _is_set(raw.get(key)) else None
                ),
            )
            for key in SYSTEM_KEY_MAPPING
        ]
    )


async def reveal_system_setting(
    key: str,
    bao_client: OpenBaoClient,
) -> SystemSettingRevealResponse:
    """Return a single system setting's raw value.

    Args:
        key: An ``OZ_*`` key from ``SYSTEM_KEY_MAPPING``.
        bao_client: An authenticated :class:`OpenBaoClient`.

    Returns:
        A :class:`SystemSettingRevealResponse` with the raw stored value.

    Raises:
        NotFoundError: If *key* is unknown or not set in the system secret.
        OpenBaoConnectionError: If OpenBao is unreachable.
    """
    if key not in SYSTEM_KEY_MAPPING:
        raise NotFoundError(f"Unknown system setting key: {key}.")
    raw = await bao_client.read_system_config()
    value = raw.get(key)
    if value is None:
        raise NotFoundError(f"System setting {key} is not set.")
    return SystemSettingRevealResponse(key=key, value=str(value))
