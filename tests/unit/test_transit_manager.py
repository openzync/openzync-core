"""Unit tests for TransitManager — mock-based, no OpenBao required.

Verifies:
- Each encrypt/decrypt method calls the underlying OpenBao client correctly.
- Additional authenticated data (AAD) is passed as context where appropriate.
- rotate_all_keys handles success and failure for all three keys.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.transit import (
    ORG_API_KEY_KEY,
    PII_ENCRYPTION_KEY,
    WEBHOOK_SECRET_KEY,
    TransitManager,
)


@pytest.fixture
def mock_bao() -> AsyncMock:
    """Create a mock OpenBaoClient with transit methods."""
    client = AsyncMock()
    client.encrypt_data = AsyncMock(return_value="vault:v1:ciphertext")
    client.decrypt_data = AsyncMock(
        side_effect=lambda key, ct, context=None: f"decrypted-{key}",
    )
    client.rotate_encryption_key = AsyncMock()
    return client


@pytest.fixture
def transit(mock_bao: AsyncMock) -> TransitManager:
    """Create a TransitManager backed by the mock client."""
    return TransitManager(mock_bao)


class TestOrgApiKeyEncryption:
    """Encrypt/decrypt for organisation API keys."""

    @pytest.mark.asyncio
    async def test_encrypt_org_api_key_calls_bao(
        self,
        transit: TransitManager,
        mock_bao: AsyncMock,
    ) -> None:
        org_id = uuid4()
        api_key = "sk-original-value"

        result = await transit.encrypt_org_api_key(org_id, api_key)

        mock_bao.encrypt_data.assert_called_once_with(
            ORG_API_KEY_KEY,
            api_key,
            context=str(org_id),
        )
        assert result == "vault:v1:ciphertext"

    @pytest.mark.asyncio
    async def test_decrypt_org_api_key_calls_bao(
        self,
        transit: TransitManager,
        mock_bao: AsyncMock,
    ) -> None:
        org_id = uuid4()
        ciphertext = "vault:v1:ciphertext"

        result = await transit.decrypt_org_api_key(org_id, ciphertext)

        mock_bao.decrypt_data.assert_called_once_with(
            ORG_API_KEY_KEY,
            ciphertext,
            context=str(org_id),
        )
        assert "decrypted" in result

    @pytest.mark.asyncio
    async def test_roundtrip_with_different_orgs(
        self,
        transit: TransitManager,
        mock_bao: AsyncMock,
    ) -> None:
        """Different org IDs should produce different context AAD."""
        org_a = uuid4()
        org_b = uuid4()

        await transit.encrypt_org_api_key(org_a, "key-a")
        await transit.encrypt_org_api_key(org_b, "key-b")

        assert mock_bao.encrypt_data.call_count == 2
        call_args = mock_bao.encrypt_data.call_args_list
        assert call_args[0].kwargs["context"] != call_args[1].kwargs["context"]


class TestWebhookSecretEncryption:
    """Encrypt/decrypt for webhook signing secrets."""

    @pytest.mark.asyncio
    async def test_encrypt_webhook_secret(
        self,
        transit: TransitManager,
        mock_bao: AsyncMock,
    ) -> None:
        webhook_id = uuid4()
        result = await transit.encrypt_webhook_secret(webhook_id, "whsec-test")
        mock_bao.encrypt_data.assert_called_once_with(
            WEBHOOK_SECRET_KEY,
            "whsec-test",
            context=str(webhook_id),
        )
        assert result == "vault:v1:ciphertext"

    @pytest.mark.asyncio
    async def test_decrypt_webhook_secret(
        self,
        transit: TransitManager,
        mock_bao: AsyncMock,
    ) -> None:
        webhook_id = uuid4()
        result = await transit.decrypt_webhook_secret(webhook_id, "vault:v1:ct")
        mock_bao.decrypt_data.assert_called_once_with(
            WEBHOOK_SECRET_KEY,
            "vault:v1:ct",
            context=str(webhook_id),
        )


class TestPIIEncryption:
    """Encrypt/decrypt for PII data."""

    @pytest.mark.asyncio
    async def test_encrypt_pii(
        self,
        transit: TransitManager,
        mock_bao: AsyncMock,
    ) -> None:
        user_id = uuid4()
        result = await transit.encrypt_pii(user_id, "sensitive-data")
        mock_bao.encrypt_data.assert_called_once_with(
            PII_ENCRYPTION_KEY,
            "sensitive-data",
            context=str(user_id),
        )

    @pytest.mark.asyncio
    async def test_decrypt_pii(
        self,
        transit: TransitManager,
        mock_bao: AsyncMock,
    ) -> None:
        user_id = uuid4()
        result = await transit.decrypt_pii(user_id, "vault:v1:encrypted-pii")
        mock_bao.decrypt_data.assert_called_once_with(
            PII_ENCRYPTION_KEY,
            "vault:v1:encrypted-pii",
            context=str(user_id),
        )


class TestKeyRotation:
    """rotate_all_keys behaviour."""

    @pytest.mark.asyncio
    async def test_rotate_all_keys_calls_all_three(
        self,
        transit: TransitManager,
        mock_bao: AsyncMock,
    ) -> None:
        results = await transit.rotate_all_keys()

        assert mock_bao.rotate_encryption_key.call_count == 3
        called_keys = [call.args[0] for call in mock_bao.rotate_encryption_key.call_args_list]
        assert ORG_API_KEY_KEY in called_keys
        assert WEBHOOK_SECRET_KEY in called_keys
        assert PII_ENCRYPTION_KEY in called_keys
        assert all(v == "rotated" for v in results.values())

    @pytest.mark.asyncio
    async def test_rotate_all_keys_handles_failures(
        self,
        transit: TransitManager,
        mock_bao: AsyncMock,
    ) -> None:
        """If one key rotation fails, others still proceed."""
        mock_bao.rotate_encryption_key = AsyncMock(
            side_effect=[None, RuntimeError("key not found"), None],
        )

        results = await transit.rotate_all_keys()

        assert results[ORG_API_KEY_KEY] == "rotated"
        assert "failed" in results[WEBHOOK_SECRET_KEY]
        assert results[PII_ENCRYPTION_KEY] == "rotated"
