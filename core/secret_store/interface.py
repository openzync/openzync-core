"""Key Encryption Backend — abstract interface for encrypting secrets at rest.

The :class:`KeyEncryptionBackend` ABC defines two methods — ``encrypt`` and
``decrypt`` — that concrete implementations must provide.  The ``context``
parameter supplies associated data (e.g. the org ID) for AEAD binding, so a
ciphertext stolen from one org cannot be decrypted with a context for a
different org (assuming the backend supports AAD).

Usage::

    class MyBackend(KeyEncryptionBackend):
        async def encrypt(self, plaintext: str, context: str) -> str: ...
        async def decrypt(self, ciphertext: str, context: str) -> str: ...
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class KeyEncryptionBackend(ABC):
    """Abstract interface for encrypting/decrypting secrets at rest.

    Implementations must be thread-safe and idempotent — the same
    ``(plaintext, context)`` should produce the same ciphertext only when
    determinism is intended (e.g. deterministic AEAD).

    Args:
        master_key: Backend-specific key material (Fernet key, Vault token,
            KMS key ID, etc.).
    """

    def __init__(self, master_key: str) -> None:
        self._master_key = master_key

    @abstractmethod
    async def encrypt(self, plaintext: str, context: str) -> str:
        """Encrypt ``plaintext`` bound to ``context``.

        Args:
            plaintext: The secret value to encrypt (e.g. an API key).
            context: Associated data (e.g. ``str(org_id)``) for AEAD
                binding.  The same context must be passed to ``decrypt``.

        Returns:
            A portable, printable ciphertext string (e.g. Fernet token).

        Raises:
            EncryptionError: If encryption fails.
        """

    @abstractmethod
    async def decrypt(self, ciphertext: str, context: str) -> str:
        """Decrypt ``ciphertext`` previously produced by ``encrypt``.

        Args:
            ciphertext: The ciphertext string returned by ``encrypt``.
            context: The same associated data that was used during
                encryption.

        Returns:
            The original plaintext string.

        Raises:
            DecryptionError: If decryption fails (wrong key, wrong
                context, corrupted ciphertext).
            NotEncryptedError: If the ciphertext does not look like
                an encrypted value (e.g. plaintext stored before
                encryption was enabled).
        """
