"""Unit tests for ``core.org_config`` resolution engine.

All tests mock OpenBao and Redis so they run fast and in isolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from core.org_config import (
    build_cache_key,
    get_org_config,
    update_org_config,
)
from schemas.organization_config import (
    OrgConfigBase,
    UpdateOrgConfigRequest,
)


@pytest.fixture
def org_id() -> UUID:
    return uuid4()


@pytest.fixture
def mock_bao_client() -> AsyncMock:
    """Return an AsyncMock that behaves as an authenticated OpenBaoClient."""
    client = AsyncMock()
    client.read_org_config = AsyncMock(return_value={})
    client.write_org_config = AsyncMock()
    return client


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    return redis


class TestGetOrgConfig:
    """Tests for the top-level config fetch function."""

    async def test_fresh_fetch_from_openbao(
        self,
        org_id: UUID,
        mock_bao_client: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """When cache is empty, config should be fetched from OpenBao."""
        mock_bao_client.read_org_config.return_value = {
            "llm_backend": "anthropic",
            "llm_model": "claude-3",
        }

        config = await get_org_config(
            org_id, redis=mock_redis, bao_client=mock_bao_client
        )

        assert isinstance(config, OrgConfigBase)
        assert config.llm_backend == "anthropic"
        assert config.llm_model == "claude-3"

        # Cache should have been warmed
        mock_redis.setex.assert_awaited_once()
        mock_bao_client.read_org_config.assert_awaited_once_with(org_id)

    async def test_cache_hit(
        self,
        org_id: UUID,
        mock_bao_client: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """When cache has data, OpenBao should not be queried."""
        mock_redis.get.return_value = '{"llm_backend": "openai"}'

        config = await get_org_config(
            org_id, redis=mock_redis, bao_client=mock_bao_client
        )

        assert config.llm_backend == "openai"
        # OpenBao should NOT have been called
        mock_bao_client.read_org_config.assert_not_called()

    async def test_cache_corrupted_data_falls_back_to_openbao(
        self,
        org_id: UUID,
        mock_bao_client: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """Corrupted cache JSON should not crash — fall back to OpenBao."""
        mock_redis.get.return_value = "not-valid-json{{{"
        mock_bao_client.read_org_config.return_value = {"llm_backend": "ollama"}

        config = await get_org_config(
            org_id, redis=mock_redis, bao_client=mock_bao_client
        )

        assert config.llm_backend == "ollama"  # fell back to OpenBao

    async def test_empty_openbao_config_returns_all_none(
        self,
        org_id: UUID,
        mock_bao_client: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """An empty OpenBao config should result in all-None fields (no env defaults)."""
        mock_bao_client.read_org_config.return_value = {}

        config = await get_org_config(
            org_id, redis=mock_redis, bao_client=mock_bao_client
        )

        # Every field should be None — no env-var fallback
        for field_name in OrgConfigBase.model_fields:
            assert getattr(config, field_name) is None, (
                f"Expected {field_name} to be None, got {getattr(config, field_name)!r}"
            )

    async def test_skip_cache_flag(
        self,
        org_id: UUID,
        mock_bao_client: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """skip_cache=True should bypass Redis even when it's available."""
        mock_redis.get.return_value = '{"llm_backend": "cached"}'
        mock_bao_client.read_org_config.return_value = {"llm_backend": "from_openbao"}

        config = await get_org_config(
            org_id, redis=mock_redis, bao_client=mock_bao_client, skip_cache=True
        )

        assert config.llm_backend == "from_openbao"  # from OpenBao, not cache
        mock_redis.get.assert_not_awaited()

    async def test_no_redis_skips_cache_entirely(
        self,
        org_id: UUID,
        mock_bao_client: AsyncMock,
    ) -> None:
        """When redis is None, always fetch from OpenBao (no cache layer)."""
        mock_bao_client.read_org_config.return_value = {"llm_backend": "no-cache"}

        config = await get_org_config(
            org_id, redis=None, bao_client=mock_bao_client
        )

        assert config.llm_backend == "no-cache"
        mock_bao_client.read_org_config.assert_awaited_once_with(org_id)

    async def test_requires_client(
        self,
        org_id: UUID,
    ) -> None:
        """When bao_client is None, an error should be raised."""
        from core.openbao_exceptions import OpenBaoConnectionError

        with pytest.raises(OpenBaoConnectionError):
            await get_org_config(org_id, bao_client=None)


class TestUpdateOrgConfig:
    """Tests for the config update + cache invalidation flow."""

    async def test_partial_update_merges_correctly(
        self,
        org_id: UUID,
        mock_bao_client: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """A partial update should merge new fields into existing config."""
        mock_bao_client.read_org_config.return_value = {
            "llm_backend": "ollama",
            "llm_model": "llama3",
        }

        update = UpdateOrgConfigRequest(llm_backend="openai")
        resolved = await update_org_config(
            org_id, update, bao_client=mock_bao_client, redis=mock_redis
        )

        assert resolved.llm_backend == "openai"
        # Should have written merged config to OpenBao
        mock_bao_client.write_org_config.assert_awaited_once_with(
            org_id,
            {"llm_backend": "openai", "llm_model": "llama3"},
        )
        # Cache should have been invalidated
        mock_redis.delete.assert_awaited_once()

    async def test_none_field_removes_key(
        self,
        org_id: UUID,
        mock_bao_client: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """Setting a field to None should remove it from stored config."""
        mock_bao_client.read_org_config.return_value = {
            "llm_backend": "ollama",
            "llm_model": "llama3",
        }

        update = UpdateOrgConfigRequest(llm_backend=None)
        resolved = await update_org_config(
            org_id, update, bao_client=mock_bao_client, redis=mock_redis
        )

        # write_org_config should have been called WITHOUT llm_backend
        mock_bao_client.write_org_config.assert_awaited_once_with(
            org_id,
            {"llm_model": "llama3"},
        )
        # Other fields preserved
        assert resolved.llm_model == "llama3"
        assert resolved.llm_backend is None

    async def test_cache_invalidation_failure_does_not_raise(
        self,
        org_id: UUID,
        mock_bao_client: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """A cache invalidation failure should log but not fail the operation."""
        mock_bao_client.read_org_config.return_value = {"llm_backend": "ollama"}
        mock_redis.delete.side_effect = RuntimeError("Redis down")

        update = UpdateOrgConfigRequest(llm_backend="openai")
        # Should not raise despite Redis failure
        resolved = await update_org_config(
            org_id, update, bao_client=mock_bao_client, redis=mock_redis
        )
        assert resolved.llm_backend == "openai"


class TestBuildCacheKey:
    """Tests for the cache key builder."""

    def test_cache_key_format(self) -> None:
        """Cache key should follow the org_config:<uuid> pattern."""
        oid = UUID("12345678-1234-5678-1234-567812345678")
        assert build_cache_key(oid) == "org_config:12345678-1234-5678-1234-567812345678"
