"""Local symmetric encryption — Fernet with per-purpose HKDF subkeys.

Replaces the OpenBao Transit backend.  Keys are derived from
``OZ_SECRET_KEY`` (``OZ_SECRET_KEY_PREVIOUS`` decrypts only, for rotation)
via HKDF-SHA256, so no key material is ever stored.

Token format: ``v1:<purpose>:<fernet-token>``.  Ciphertexts in the old
OpenBao Transit format (``vault:v1:...``) are detected on decrypt and
rejected with :class:`ValidationError` — the secret must be re-created
(break + recreate accepted, no rewrap).
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from core.exceptions import ValidationError

_TOKEN_PREFIX = "v1"
_LEGACY_TRANSIT_PREFIX = "vault:"
_AAD_SEP = "\x00"


def is_crypto_ciphertext(value: str | None) -> bool:
    """Return whether *value* is a new-format ``v1:<purpose>:`` ciphertext.

    Args:
        value: The stored secret value (may be legacy plaintext).

    Returns:
        True when the value carries the ``v1:`` envelope.
    """
    return bool(value) and value.startswith(f"{_TOKEN_PREFIX}:")


def _derive_key(master: str, purpose: str) -> bytes:
    """Derive a Fernet key (urlsafe-b64, 32 bytes) for *purpose*.

    Args:
        master: The ``OZ_SECRET_KEY`` / ``OZ_SECRET_KEY_PREVIOUS`` value.
        purpose: Encryption domain (e.g. ``"webhook-secret"``).

    Returns:
        A Fernet-ready key.
    """
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"openzync-crypto-v1",
        info=f"purpose:{purpose}".encode(),
    ).derive(master.encode())
    return base64.urlsafe_b64encode(raw)


def _masters() -> tuple[str, str | None]:
    """Return ``(current, previous-or-None)`` master secrets from settings."""
    from core.config import get_settings  # noqa: PLC0415 — lazy, avoids import cycle

    settings = get_settings()
    return settings.SECRET_KEY, settings.SECRET_KEY_PREVIOUS or None


def encrypt(purpose: str, aad: str, plaintext: str) -> str:
    """Encrypt *plaintext*, binding it to *aad*.

    Args:
        purpose: Encryption domain — selects the derived subkey and is
            embedded in the envelope.
        aad: Additional authenticated data (e.g. the endpoint id) that
            must match at decrypt time.
        plaintext: The secret to encrypt.

    Returns:
        A ``v1:<purpose>:<token>`` string for storage.

    Raises:
        ExternalServiceError: Never — encryption is local; misconfiguration
            surfaces as a fail-fast error from ``get_settings``.
    """
    current, _ = _masters()
    fernet = Fernet(_derive_key(current, purpose))
    token = fernet.encrypt(f"{aad}{_AAD_SEP}{plaintext}".encode()).decode()
    return f"{_TOKEN_PREFIX}:{purpose}:{token}"


def decrypt(purpose: str, aad: str, token: str) -> str:
    """Decrypt a token produced by :func:`encrypt`.

    Tries the current key first, then ``OZ_SECRET_KEY_PREVIOUS`` (rotation
    window).  The embedded purpose and AAD must both match.

    Args:
        purpose: Expected encryption domain.
        aad: The AAD value bound at encrypt time.
        token: The stored ``v1:<purpose>:`` ciphertext.

    Returns:
        The plaintext secret.

    Raises:
        ValidationError: If *token* is a legacy ``vault:v1:`` ciphertext
            (re-create the secret) or the envelope purpose mismatches.
        InvalidToken: If neither key decrypts it (wrong AAD or corruption).
    """
    if token.startswith(_LEGACY_TRANSIT_PREFIX):
        raise ValidationError("re-create webhook secret")
    parts = token.split(":", 2)
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX or parts[1] != purpose:
        raise ValidationError("re-create webhook secret")
    current, previous = _masters()
    last: Exception | None = None
    for master in (current, *([previous] if previous else [])):
        try:
            raw = Fernet(_derive_key(master, purpose)).decrypt(parts[2].encode())
        except InvalidToken as exc:
            last = exc
            continue
        bound_aad, sep, plaintext = raw.decode().partition(_AAD_SEP)
        if not sep or bound_aad != aad:
            raise InvalidToken("AAD mismatch") from None
        return plaintext
    raise InvalidToken("cannot decrypt with current or previous key") from last
