"""High-level Transit encryption manager for OpenBao.

Provides typed encrypt/decrypt methods for specific data contexts (org API
keys, webhook secrets, PII) and manages encryption key lifecycle.

Usage::

    from core.openbao import OpenBaoClient
    from core.transit import TransitManager

    async with OpenBaoClient(addr, role_id, secret_id) as bao:
        transit = TransitManager(bao)

        # Encrypt an org's LLM API key before storing in DB
        encrypted = await transit.encrypt_org_api_key(org_id, "sk-...")

        # Decrypt when needed
        plaintext = await transit.decrypt_org_api_key(org_id, encrypted)
"""

from __future__ import annotations

import structlog
from typing import Any
from uuid import UUID

from core.openbao import OpenBaoClient

logger = structlog.get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Encryption key names (used as Transit engine key identifiers)
# ═══════════════════════════════════════════════════════════════════════════════

ORG_API_KEY_KEY = "org-api-key"
"""Transit key for encrypting organisation LLM/embedding API keys."""

WEBHOOK_SECRET_KEY = "webhook-secret"
"""Transit key for encrypting webhook signing secrets."""

PII_ENCRYPTION_KEY = "pii-encryption"
"""Transit key for encrypting PII data at rest."""


# ═══════════════════════════════════════════════════════════════════════════════
# TransitManager
# ═══════════════════════════════════════════════════════════════════════════════


class TransitManager:
    """High-level wrapper around OpenBao Transit encryption.

    Manages key lifecycle and provides typed encrypt/decrypt for specific
    data contexts.  All methods are async and require an authenticated
    :class:`OpenBaoClient` instance.

    Args:
        bao_client: An authenticated OpenBao client.
    """

    def __init__(self, bao_client: OpenBaoClient) -> None:
        self._bao = bao_client

    # ── Org API keys ───────────────────────────────────────────────────────

    async def encrypt_org_api_key(
        self,
        org_id: UUID,
        api_key: str,
    ) -> str:
        """Encrypt an organisation API key (LLM, embedding, etc.).

        Args:
            org_id: Organisation UUID (used as AAD context).
            api_key: The plaintext API key.

        Returns:
            OpenBao ciphertext string.
        """
        return await self._bao.encrypt_data(
            ORG_API_KEY_KEY,
            api_key,
            context=str(org_id),
        )

    async def decrypt_org_api_key(
        self,
        org_id: UUID,
        ciphertext: str,
    ) -> str:
        """Decrypt an organisation API key.

        Args:
            org_id: Organisation UUID (must match the AAD used at encryption).
            ciphertext: Ciphertext from :meth:`encrypt_org_api_key`.

        Returns:
            Decrypted plaintext API key.
        """
        return await self._bao.decrypt_data(
            ORG_API_KEY_KEY,
            ciphertext,
            context=str(org_id),
        )

    # ── Webhook secrets ────────────────────────────────────────────────────

    async def encrypt_webhook_secret(
        self,
        webhook_id: UUID,
        secret: str,
    ) -> str:
        """Encrypt a webhook signing secret.

        Args:
            webhook_id: Webhook UUID (used as AAD context).
            secret: The plaintext webhook signing secret.

        Returns:
            OpenBao ciphertext string.
        """
        return await self._bao.encrypt_data(
            WEBHOOK_SECRET_KEY,
            secret,
            context=str(webhook_id),
        )

    async def decrypt_webhook_secret(
        self,
        webhook_id: UUID,
        ciphertext: str,
    ) -> str:
        """Decrypt a webhook signing secret.

        Args:
            webhook_id: Webhook UUID (must match the AAD used at encryption).
            ciphertext: Ciphertext from :meth:`encrypt_webhook_secret`.

        Returns:
            Decrypted plaintext secret.
        """
        return await self._bao.decrypt_data(
            WEBHOOK_SECRET_KEY,
            ciphertext,
            context=str(webhook_id),
        )

    # ── PII / User data ────────────────────────────────────────────────────

    async def encrypt_pii(
        self,
        user_id: UUID,
        plaintext: str,
    ) -> str:
        """Encrypt personally identifiable information (PII).

        Args:
            user_id: User UUID (used as AAD context).
            plaintext: The PII data to encrypt.

        Returns:
            OpenBao ciphertext string.
        """
        return await self._bao.encrypt_data(
            PII_ENCRYPTION_KEY,
            plaintext,
            context=str(user_id),
        )

    async def decrypt_pii(
        self,
        user_id: UUID,
        ciphertext: str,
    ) -> str:
        """Decrypt PII data.

        Args:
            user_id: User UUID (must match the AAD used at encryption).
            ciphertext: Ciphertext from :meth:`encrypt_pii`.

        Returns:
            Decrypted plaintext.
        """
        return await self._bao.decrypt_data(
            PII_ENCRYPTION_KEY,
            ciphertext,
            context=str(user_id),
        )

    # ── Key lifecycle ──────────────────────────────────────────────────────

    async def rotate_all_keys(self) -> dict[str, Any]:
        """Rotate all known encryption keys.

        New data will use new key versions; old data remains decryptable.

        Returns:
            Dict mapping key names to rotation outcome messages.
        """
        results: dict[str, Any] = {}
        for key_name in (ORG_API_KEY_KEY, WEBHOOK_SECRET_KEY, PII_ENCRYPTION_KEY):
            try:
                await self._bao.rotate_encryption_key(key_name)
                results[key_name] = "rotated"
                logger.info("transit.key_rotated", key=key_name)
            except Exception as exc:  # noqa: BLE001
                results[key_name] = f"failed: {exc}"
                logger.exception(
                    "transit.key_rotate_failed",
                    key=key_name,
                    error=str(exc),
                )
        return results
