"""Unit tests: ``TransitManager.verify_webhook_secret_key`` (B2b).

- ``key_exists`` raising ``OpenBaoAuthError`` (403 metadata-read denial) →
  returns normally with a warning, no raise.
- ``key_exists`` returning ``False`` → raises ``ExternalServiceError`` (502).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.exceptions import ExternalServiceError
from core.openbao_exceptions import OpenBaoAuthError
from core.transit import TransitManager

pytestmark = pytest.mark.unit


def _manager(key_exists: AsyncMock) -> tuple[TransitManager, AsyncMock]:
    bao = AsyncMock()
    bao.key_exists = key_exists
    return TransitManager(bao), bao


class TestVerifyWebhookSecretKey:
    """Startup gate for the ``webhook-secret`` Transit key."""

    @pytest.mark.asyncio
    async def test_auth_error_returns_normally(self) -> None:
        """403 on key metadata read → warn and continue, no raise."""
        manager, _ = _manager(
            AsyncMock(side_effect=OpenBaoAuthError("permission denied", status_code=403))
        )

        assert await manager.verify_webhook_secret_key() is None

    @pytest.mark.asyncio
    async def test_missing_key_raises_502(self) -> None:
        """``key_exists → False`` → ``ExternalServiceError`` (JSON 502)."""
        manager, _ = _manager(AsyncMock(return_value=False))

        with pytest.raises(ExternalServiceError) as exc_info:
            await manager.verify_webhook_secret_key()

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail.get("key") == "webhook-secret"

    @pytest.mark.asyncio
    async def test_existing_key_returns_normally(self) -> None:
        """``key_exists → True`` → returns normally."""
        manager, _ = _manager(AsyncMock(return_value=True))

        assert await manager.verify_webhook_secret_key() is None
