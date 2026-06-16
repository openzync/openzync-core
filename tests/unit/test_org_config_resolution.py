"""Unit tests for ``core.org_config`` resolution engine.

All tests mock the database and Redis so they run fast and in isolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
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
def mock_db() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    return redis


class TestGetOrgConfig:
    """Tests for the top-level config fetch function."""

    @patch("core.org_config.OrganizationRepository")
    async def test_fresh_fetch_from_db(
        self,
        mock_repo_cls: MagicMock,
        org_id: UUID,
        mock_db: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """When cache is empty, config should be fetched from the DB."""
        mock_repo = AsyncMock()
        mock_repo.get_config.return_value = {"llm_backend": "anthropic", "llm_model": "claude-3"}
        mock_repo_cls.return_value = mock_repo

        config = await get_org_config(org_id, mock_db, redis=mock_redis)

        assert isinstance(config, OrgConfigBase)
        assert config.llm_backend == "anthropic"
        assert config.llm_model == "claude-3"

        # Cache should have been warmed
        mock_redis.setex.assert_awaited_once()
        mock_repo.get_config.assert_awaited_once_with(org_id)

    @patch("core.org_config.OrganizationRepository")
    async def test_cache_hit(
        self,
        mock_repo_cls: MagicMock,
        org_id: UUID,
        mock_db: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """When cache has data, DB should not be queried."""
        mock_redis.get.return_value = '{"llm_backend": "openai"}'

        config = await get_org_config(org_id, mock_db, redis=mock_redis)

        assert config.llm_backend == "openai"
        # DB should NOT have been called
        mock_repo_cls.assert_not_called()

    @patch("core.org_config.OrganizationRepository")
    async def test_cache_corrupted_data_falls_back_to_db(
        self,
        mock_repo_cls: MagicMock,
        org_id: UUID,
        mock_db: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """Corrupted cache JSON should not crash — fall back to DB."""
        mock_redis.get.return_value = "not-valid-json{{{"
        mock_repo = AsyncMock()
        mock_repo.get_config.return_value = {"llm_backend": "ollama"}
        mock_repo_cls.return_value = mock_repo

        config = await get_org_config(org_id, mock_db, redis=mock_redis)

        assert config.llm_backend == "ollama"  # fell back to DB

    @patch("core.org_config.OrganizationRepository")
    async def test_empty_db_config_returns_all_none(
        self,
        mock_repo_cls: MagicMock,
        org_id: UUID,
        mock_db: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """An empty DB config should result in all-None fields (no env defaults)."""
        mock_repo = AsyncMock()
        mock_repo.get_config.return_value = {}
        mock_repo_cls.return_value = mock_repo

        config = await get_org_config(org_id, mock_db, redis=mock_redis)

        # Every field should be None — no env-var fallback
        for field_name in OrgConfigBase.model_fields:
            assert getattr(config, field_name) is None, (
                f"Expected {field_name} to be None, got {getattr(config, field_name)!r}"
            )

    @patch("core.org_config.OrganizationRepository")
    async def test_skip_cache_flag(
        self,
        mock_repo_cls: MagicMock,
        org_id: UUID,
        mock_db: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """skip_cache=True should bypass Redis even when it's available."""
        mock_redis.get.return_value = '{"llm_backend": "cached"}'
        mock_repo = AsyncMock()
        mock_repo.get_config.return_value = {"llm_backend": "from_db"}
        mock_repo_cls.return_value = mock_repo

        config = await get_org_config(
            org_id, mock_db, redis=mock_redis, skip_cache=True
        )

        assert config.llm_backend == "from_db"  # from DB, not cache
        mock_redis.get.assert_not_awaited()

    @patch("core.org_config.OrganizationRepository")
    async def test_no_redis_skips_cache_entirely(
        self,
        mock_repo_cls: MagicMock,
        org_id: UUID,
        mock_db: AsyncMock,
    ) -> None:
        """When redis is None, always fetch from DB (no cache layer)."""
        mock_repo = AsyncMock()
        mock_repo.get_config.return_value = {"llm_backend": "no-cache"}
        mock_repo_cls.return_value = mock_repo

        config = await get_org_config(org_id, mock_db, redis=None)

        assert config.llm_backend == "no-cache"
        mock_repo.get_config.assert_awaited_once_with(org_id)


class TestUpdateOrgConfig:
    """Tests for the config update + cache invalidation flow."""

    @patch("core.org_config.OrganizationRepository")
    async def test_partial_update_merges_correctly(
        self,
        mock_repo_cls: MagicMock,
        org_id: UUID,
        mock_db: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """A partial update should merge new fields into existing config."""
        mock_repo = AsyncMock()
        mock_repo.get_config.return_value = {"llm_backend": "ollama", "llm_model": "llama3"}
        mock_repo.update_config.return_value = {
            "llm_backend": "openai",
            "llm_model": "llama3",
        }
        mock_repo_cls.return_value = mock_repo

        update = UpdateOrgConfigRequest(llm_backend="openai")
        resolved = await update_org_config(org_id, update, mock_db, redis=mock_redis)

        assert resolved.llm_backend == "openai"
        # Cache should have been invalidated
        mock_redis.delete.assert_awaited_once()
        # Should have re-fetched from DB after update
        assert mock_repo.get_config.await_count >= 1

    @patch("core.org_config.OrganizationRepository")
    async def test_none_field_removes_key(
        self,
        mock_repo_cls: MagicMock,
        org_id: UUID,
        mock_db: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """Setting a field to None should remove it from stored config."""
        mock_repo = AsyncMock()
        mock_repo.get_config.return_value = {"llm_backend": "ollama", "llm_model": "llama3"}
        mock_repo_cls.return_value = mock_repo

        update = UpdateOrgConfigRequest(llm_backend=None)
        await update_org_config(org_id, update, mock_db, redis=mock_redis)

        # update_config should have been called WITHOUT llm_backend
        call_args = mock_repo.update_config.await_args
        assert call_args is not None
        stored = call_args[0][1] if len(call_args[0]) > 1 else call_args[1][1]
        assert "llm_backend" not in stored
        assert stored.get("llm_model") == "llama3"  # other fields preserved


class TestBuildCacheKey:
    """Tests for the cache key builder."""

    def test_cache_key_format(self) -> None:
        """Cache key should follow the org_config:<uuid> pattern."""
        oid = UUID("12345678-1234-5678-1234-567812345678")
        assert build_cache_key(oid) == "org_config:12345678-1234-5678-1234-567812345678"
