"""Unit tests for dependencies/platform_config.py — platform config resolver."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import Request

pytestmark = pytest.mark.unit

ORG_UUID = UUID("00000000-0000-0000-0000-000000000001")
ORG_ID_STR = "00000000-0000-0000-0000-000000000001"


class TestGetPlatformConfig:
    """get_platform_config: resolves platform config from Redis/DB."""

    @pytest.mark.asyncio
    async def test_returns_org_config_from_resolver(self) -> None:
        """Valid user_id → delegates to core.platform_config.get_platform_config."""
        from dependencies.platform_config import get_platform_config

        request = MagicMock(spec=Request)
        request.state.user_id = ORG_ID_STR
        request.app.state.redis = MagicMock()
        mock_db = AsyncMock()

        fake_config = MagicMock()
        fake_config.llm_model = "gpt-4"

        # _get_platform_config is imported locally inside get_platform_config() as
        #   from core.platform_config import get_platform_config as _get_platform_config
        # so we patch core.platform_config.get_platform_config directly.
        with patch(
            "core.platform_config.get_platform_config",
            new_callable=AsyncMock,
        ) as mock_resolver:
            mock_resolver.return_value = fake_config

            result = await get_platform_config(request, mock_db, ORG_ID_STR)

            mock_resolver.assert_awaited_once_with(
                ORG_UUID,
                redis=request.app.state.redis,
                db=mock_db,
            )
            assert result == fake_config
            assert result.llm_model == "gpt-4"

    @pytest.mark.asyncio
    async def test_handles_none_redis(self) -> None:
        """When redis is None, resolver gets None redis + the request db."""
        from dependencies.platform_config import get_platform_config

        request = MagicMock(spec=Request)
        request.state.user_id = ORG_ID_STR
        request.app.state.redis = None
        mock_db = AsyncMock()

        with patch(
            "core.platform_config.get_platform_config",
            new_callable=AsyncMock,
        ) as mock_resolver:
            mock_resolver.return_value = MagicMock()

            await get_platform_config(request, mock_db, ORG_ID_STR)

            mock_resolver.assert_awaited_once_with(
                ORG_UUID, redis=None, db=mock_db
            )
