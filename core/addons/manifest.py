"""Addon manifest parsing and validation (``addon.toml``).

Every addon bundle carries an ``addon.toml`` at its top level::

    [addon]
    name = "my-addon"
    version = "1.2.0"
    min_core_version = "1.0.0"
    hooks = ["router", "worker"]
    permissions = ["project:read"]
    tables = ["my_addon_items"]

    [signing]
    signature = "<ed25519 hex over the unsigned manifest bytes>"

Validation is fail-closed: any missing field, unknown permission, core
version mismatch, or bad signature raises :class:`ValidationError` and the
loader records ``app.state.degraded["addon:{id}"]`` instead of loading.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass

from core._version import __version__
from core.exceptions import ValidationError

logger = logging.getLogger(__name__)

REQUIRED_HOOKS: frozenset[str] = frozenset(
    {
        "router",
        "service",
        "middleware",
        "worker",
        "graph_backend",
        "embedder",
        "llm",
        "health",
    }
)
"""Hook names an addon may declare. Unknown hooks are rejected."""

ADDON_ID_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
"""Addon ids: lowercase slug, 2–64 chars. Used for module names and URLs."""

TABLE_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
"""Allowlist for addon-owned table names (purge path only)."""


@dataclass(frozen=True, slots=True)
class AddonManifest:
    """Validated ``addon.toml`` contents."""

    id: str
    version: str
    min_core_version: str
    hooks: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    signature: str = ""


def _parse_version(version: str) -> tuple[int, ...]:
    """Parse ``major.minor.patch`` into a comparable tuple.

    Args:
        version: A dotted version string.

    Returns:
        The numeric components as a tuple (non-numeric suffixes ignored).

    Raises:
        ValidationError: If no numeric component can be parsed.
    """
    parts: list[int] = []
    for chunk in version.split("."):
        digits = re.match(r"\d+", chunk.strip())
        if digits is None:
            break
        parts.append(int(digits.group(0)))
    if not parts:
        raise ValidationError(f"Invalid version string: {version!r}.")
    return tuple(parts)


def parse_manifest(raw: bytes) -> AddonManifest:
    """Parse and validate ``addon.toml`` bytes (signature NOT verified).

    Args:
        raw: The raw ``addon.toml`` file bytes.

    Returns:
        A validated :class:`AddonManifest`.

    Raises:
        ValidationError: On any missing field, bad id/version, unknown
            hook, unknown permission, unsafe table name, or a core
            version older than ``min_core_version``.
    """
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValidationError(f"addon.toml is not valid TOML: {exc}") from exc

    section = doc.get("addon")
    if not isinstance(section, dict):
        raise ValidationError("addon.toml must contain an [addon] table.")

    addon_id = section.get("name", "")
    if not isinstance(addon_id, str) or not ADDON_ID_RE.match(addon_id):
        raise ValidationError(
            f"Invalid addon name {addon_id!r}: 2-64 lowercase slug chars."
        )

    version = section.get("version", "")
    if not isinstance(version, str) or not version:
        raise ValidationError("addon.toml [addon] version is required.")
    _parse_version(version)

    min_core = str(section.get("min_core_version", "0.0.0"))
    if _parse_version(__version__) < _parse_version(min_core):
        raise ValidationError(
            f"Addon {addon_id!r} requires core >= {min_core}, running {__version__}."
        )

    hooks = tuple(section.get("hooks", []))
    unknown_hooks = set(hooks) - REQUIRED_HOOKS
    if unknown_hooks:
        raise ValidationError(f"Unknown addon hooks: {sorted(unknown_hooks)}.")

    permissions = tuple(section.get("permissions", []))
    if permissions:
        from core.rbac import ALL_PERMISSIONS

        unknown_perms = set(permissions) - set(ALL_PERMISSIONS)
        if unknown_perms:
            raise ValidationError(
                f"Unknown addon permissions: {sorted(unknown_perms)}."
            )

    tables = tuple(section.get("tables", []))
    unsafe = [t for t in tables if not TABLE_NAME_RE.match(t)]
    if unsafe:
        raise ValidationError(f"Unsafe addon table names: {unsafe}.")

    signature = doc.get("signing", {}).get("signature", "")
    if not isinstance(signature, str):
        raise ValidationError("signing.signature must be a hex string.")

    return AddonManifest(
        id=addon_id,
        version=version,
        min_core_version=min_core,
        hooks=hooks,
        permissions=permissions,
        tables=tables,
        signature=signature,
    )


def unsigned_bytes(raw: bytes) -> bytes:
    """Return manifest bytes with the ``signature`` line stripped.

    The ed25519 signature covers the manifest *without* its own signature
    line, so signing and verification share this canonicalisation.

    Args:
        raw: The raw ``addon.toml`` file bytes.

    Returns:
        The bytes with any ``signature = ...`` line removed.
    """
    return b"\n".join(
        line for line in raw.splitlines() if not line.strip().startswith(b"signature")
    )


def verify_manifest_signature(
    raw: bytes, signature_hex: str, public_key_hex: str
) -> None:
    """Verify the manifest ed25519 signature, fail-closed.

    Args:
        raw: The raw ``addon.toml`` bytes (signature line included).
        signature_hex: Hex-encoded 64-byte ed25519 signature.
        public_key_hex: Hex-encoded 32-byte ed25519 public key (platform
            signing key, sourced from OpenBao — never from the bundle).

    Raises:
        ValidationError: If the signature is missing, malformed, or does
            not verify. No verification path succeeds silently.
    """
    if not signature_hex:
        raise ValidationError("Addon bundle is unsigned — refusing to load.")
    if not public_key_hex:
        raise ValidationError("No addon signing key configured — refusing to load.")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), unsigned_bytes(raw))
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"Addon signature verification failed: {exc}") from exc
    logger.debug("addon manifest signature verified")
