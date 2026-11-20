"""Platform system settings — read-only, superadmin-only, masked list + raw reveal.

Reads the ``OZ_*`` env-backed :class:`core.config.Settings` and exposes
every known key under masking rules that never leak credentials.
No caching: reads are local attribute lookups and the endpoint is
superadmin-only.
"""

from __future__ import annotations

import re
from typing import Any

from core.exceptions import NotFoundError
from schemas.admin_system import (
    SYSTEM_SETTING_CATEGORIES,
    SystemSettingItem,
    SystemSettingRevealResponse,
    SystemSettingsResponse,
)

_MASK = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"
"""Mask rendered for secret values (8 bullet chars)."""

# note: explicit set rather than substring matching on
# PASSWORD/SECRET/KEY/TOKEN — "TOKEN" would false-positive on the
# OZ_JWT_*_TOKEN_TTL_* keys (numeric TTLs, not secrets).
_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "OZ_SECRET_KEY",
        "OZ_SECRET_KEY_PREVIOUS",
        "OZ_WEBHOOK_SIGNING_SECRET",
        "OZ_SMTP_PASSWORD",
        "OZ_SMTP_USERNAME",  # treated as a secret — pairs with the password
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
"""Matches ``://<userinfo>@`` — the userinfo of a URL.

Userinfo never contains ``/`` or ``@``.
"""

#: Every ``OZ_*`` key the settings endpoints may list/reveal.  Single
#: source of truth — mirrors the ``Settings`` env bindings.
SYSTEM_SETTINGS_KEYS: tuple[str, ...] = tuple(SYSTEM_SETTING_CATEGORIES)


def _is_set(value: Any) -> bool:
    """True unless the raw value is absent or an empty string."""
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


def _read_raw() -> dict[str, Any]:
    """Read every known ``OZ_*`` value from the env-backed settings.

    Returns:
        A dict of ``OZ_*`` key to raw value (``None`` when unset).
    """
    from core.config import get_settings  # noqa: PLC0415 — lazy, avoids import cycle

    settings = get_settings()
    raw: dict[str, Any] = {}
    for key in SYSTEM_SETTINGS_KEYS:
        field = key.removeprefix("OZ_")
        raw[key] = getattr(settings, field, None)
    return raw


async def list_system_settings() -> SystemSettingsResponse:
    """List every known system setting with masked values.

    Reads the env-backed settings once and returns one item per known key
    (category order).  Keys that are unset report ``is_set=False`` with no
    value.

    Returns:
        A :class:`SystemSettingsResponse` with all known keys, masked.
    """
    raw = _read_raw()
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
            for key in SYSTEM_SETTINGS_KEYS
        ]
    )


async def reveal_system_setting(key: str) -> SystemSettingRevealResponse:
    """Return a single system setting's raw value.

    Args:
        key: An ``OZ_*`` key from ``SYSTEM_SETTINGS_KEYS``.

    Returns:
        A :class:`SystemSettingRevealResponse` with the raw stored value.

    Raises:
        NotFoundError: If *key* is unknown or not set.
    """
    if key not in SYSTEM_SETTINGS_KEYS:
        raise NotFoundError(f"Unknown system setting key: {key}.")
    value = _read_raw().get(key)
    if value is None:
        raise NotFoundError(f"System setting {key} is not set.")
    return SystemSettingRevealResponse(key=key, value=str(value))
