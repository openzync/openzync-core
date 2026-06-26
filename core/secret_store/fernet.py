"""Fernet-based AES-256-GCM key encryption backend.

This is the default :class:`KeyEncryptionBackend` shipped with OpenZep.
It uses the ``cryptography`` library's ``Fernet`` symmetric encryption
(AES-256 in CBC mode with a PBKDF2-derived HMAC for integrity).

**Thread safety:** ``Fernet`` is thread-safe.  A single instance can be
shared across all coroutines.

**Key format:** 44-character base64-encoded 32-byte key (Fernet standard).
Generate with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Security properties:
- AES-256-CBC with HMAC-SHA256 authentication tag.
- Time-limited (Fernet tokens carry a 60-second clock-skew window).
- Context binding via AAD is **not** supported by standard Fernet — the
  ``context`` parameter is ignored for now.  A future ``AesGcmBackend``
  or Vault-backed implementation can enforce AEAD binding at the backend
  level.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from core.secret_store.interface import KeyEncryptionBackend

logger = logging.getLogger(__name__)

# Fernet tokens start with this constant base64 prefix.
FERNET_PREFIX: str = "gAAAAA"
"""Fernet tokens always begin with ``gAAAAA`` (base64 encoding of version + timestamp).

This prefix is used to detect whether a value has already been encrypted.
"""


class FernetKeyEncryption(KeyEncryptionBackend):
    """Encrypt/decrypt secrets using AES-256-GCM via the Fernet format.

    Args:
        master_key: A 44-character base64-encoded 32-byte Fernet key.
            Generate with ``Fernet.generate_key().decode()``.
    """

    def __init__(self, master_key: str) -> None:
        super().__init__(master_key)
        self._fernet = Fernet(master_key.encode() if isinstance(master_key, str) else master_key)

    async def encrypt(self, plaintext: str, context: str) -> str:
        """Encrypt ``plaintext`` with Fernet.

        Args:
            plaintext: The secret to encrypt.
            context: Ignored by Fernet (no AAD support).  Included for
                interface compatibility.

        Returns:
            A Fernet token string (always starts with ``gAAAAA``).
        """
        # Note: context is unused — standard Fernet does not support AAD.
        # A future AES-GCM backend can bind context as AAD.
        return self._fernet.encrypt(plaintext.encode()).decode()

    async def decrypt(self, ciphertext: str, context: str) -> str:
        """Decrypt a Fernet token back to plaintext.

        Args:
            ciphertext: The Fernet token string to decrypt.
            context: Ignored by Fernet.  Included for interface compatibility.

        Returns:
            The original plaintext.

        Raises:
            NotEncryptedError: If ``ciphertext`` does not start with the
                Fernet magic prefix (i.e. it was never encrypted).
        """
        if not ciphertext.startswith(FERNET_PREFIX):
            raise NotEncryptedError(
                "Value does not appear to be encrypted (no Fernet prefix)."
            )
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as err:
            raise DecryptionError(
                f"Failed to decrypt value: {err}"
            ) from err


class NotEncryptedError(ValueError):
    """Raised when a ciphertext does not look like an encrypted value.

    This allows callers to distinguish between a value that was stored
    in plaintext before encryption was enabled (safe to return as-is)
    vs. a corrupted or tampered ciphertext.
    """


class DecryptionError(ValueError):
    """Raised when decryption fails due to a wrong key, wrong context, or
    corrupted ciphertext."""


class EncryptionError(ValueError):
    """Raised when encryption fails."""
