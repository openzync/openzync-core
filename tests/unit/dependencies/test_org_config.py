"""Unit tests for dependencies/org_config.py — org config resolver."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import Request

pytestmark = pytest.mark.unit

ORG_UUID = UUID("00000000-0000-0000-0000-000000000001")
ORG_ID_STR = "00000000-0000-0000-0000-000000000001"


class TestGetOrgConfig:
    """get_org_config: resolves org config from Redis/OpenBao."""

    @pytest.mark.asyncio
    async def test_returns_org_config_from_resolver(self) -> None:
        """Valid org_id → delegates to core.org_config.get_org_config."""
        from dependencies.org_config import get_org_config

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.app.state.openbao_client = MagicMock()
        request.app.state.redis = MagicMock()

        fake_config = MagicMock()
        fake_config.llm_model = "gpt-4"

        # _get_org_config is imported locally inside get_org_config() as
        #   from core.org_config import get_org_config as _get_org_config
        # so we patch core.org_config.get_org_config directly.
        with patch(
            "core.org_config.get_org_config",
            new_callable=AsyncMock,
        ) as mock_resolver:
            mock_resolver.return_value = fake_config

            result = await get_org_config(request, ORG_ID_STR)

            mock_resolver.assert_awaited_once_with(
                ORG_UUID,
                redis=request.app.state.redis,
                bao_client=request.app.state.openbao_client,
            )
            assert result == fake_config
            assert result.llm_model == "gpt-4"

    @pytest.mark.asyncio
    async def test_handles_none_bao_and_redis(self) -> None:
        """When bao_client and redis are None, resolver gets None args."""
        from dependencies.org_config import get_org_config

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.app.state.openbao_client = None
        request.app.state.redis = None

        with patch(
            "core.org_config.get_org_config",
            new_callable=AsyncMock,
        ) as mock_resolver:
            mock_resolver.return_value = MagicMock()

            await get_org_config(request, ORG_ID_STR)

            mock_resolver.assert_awaited_once_with(
                ORG_UUID, redis=None, bao_client=None
            )
