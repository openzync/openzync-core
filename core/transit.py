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

from typing import Any
from uuid import UUID

import structlog

from core.exceptions import ExternalServiceError
from core.openbao import OpenBaoClient
from core.openbao_exceptions import OpenBaoAuthError, OpenBaoError

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

        Raises:
            ExternalServiceError: If the Transit backend fails — mapped to
                a JSON 502 by ``register_exception_handlers``, never a
                plain-text 500.
        """
        try:
            return await self._bao.encrypt_data(
                WEBHOOK_SECRET_KEY,
                secret,
                context=str(webhook_id),
            )
        except OpenBaoError as exc:
            raise ExternalServiceError(
                "Webhook secret encryption failed (OpenBao Transit "
                f"'{WEBHOOK_SECRET_KEY}').",
                detail={"key": WEBHOOK_SECRET_KEY},
            ) from exc

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

        Raises:
            ExternalServiceError: If the Transit backend fails — mapped to
                a JSON 502 by ``register_exception_handlers``, never a
                plain-text 500.
        """
        try:
            return await self._bao.decrypt_data(
                WEBHOOK_SECRET_KEY,
                ciphertext,
                context=str(webhook_id),
            )
        except OpenBaoError as exc:
            raise ExternalServiceError(
                "Webhook secret decryption failed (OpenBao Transit "
                f"'{WEBHOOK_SECRET_KEY}').",
                detail={"key": WEBHOOK_SECRET_KEY},
            ) from exc

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

    async def verify_webhook_secret_key(self) -> None:
        """Fail fast if the ``webhook-secret`` Transit key is missing.

        Call once at startup — webhook signing cannot work without this
        key, and creating endpoints would 502 on every request.

        Raises:
            ExternalServiceError: If the key does not exist, with the
                provisioning step in the message.
        """
        try:
            exists = await self._bao.key_exists(WEBHOOK_SECRET_KEY)
        except OpenBaoAuthError as exc:
            # 403 on transit/keys/* read — key may exist, only metadata
            # read is denied. Warn and continue; webhook writes 502 if
            # the key is truly missing.
            logger.warning(
                "transit.key_unverifiable",
                key=WEBHOOK_SECRET_KEY,
                error=str(exc),
            )
            return
        if not exists:
            raise ExternalServiceError(
                "OpenBao Transit key 'webhook-secret' is missing — "
                "provision it via scripts/init_openbao.sh (section 13) "
                "before starting the API.",
                detail={"key": WEBHOOK_SECRET_KEY},
            )

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
