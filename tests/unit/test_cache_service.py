"""Unit tests for CacheService — cache-aside with stampede prevention.

Mocks the async Redis client to test logic without infrastructure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.cache_service import CacheService


@pytest.mark.unit
class TestCacheService:
    """CacheService unit tests."""

    @pytest.mark.asyncio
    async def test_no_redis_degrades_gracefully(self) -> None:
        """With redis=None, all operations return None/False."""
        cache = CacheService(redis=None)
        assert await cache.get("key") is None
        assert await cache.set("key", "val") is False
        assert await cache.delete("key") is False
        assert await cache.invalidate_user_context("org1", "user1") == 0

    @pytest.mark.asyncio
    async def test_get_returns_cached_value(self) -> None:
        """get() returns the cached string value."""
        mock_redis = AsyncMock()
        mock_redis.get.return_value = "cached_value"
        cache = CacheService(redis=mock_redis)

        result = await cache.get("test_key")
        assert result == "cached_value"
        mock_redis.get.assert_awaited_once_with("test_key")

    @pytest.mark.asyncio
    async def test_get_returns_none_on_miss(self) -> None:
        """get() returns None when key does not exist."""
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        cache = CacheService(redis=mock_redis)

        result = await cache.get("missing_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_none_on_redis_error(self) -> None:
        """get() returns None when Redis raises."""
        mock_redis = AsyncMock()
        mock_redis.get.side_effect = ConnectionError("Redis down")
        cache = CacheService(redis=mock_redis)

        result = await cache.get("test_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_stores_with_ttl(self) -> None:
        """set() calls setex with the correct TTL."""
        mock_redis = AsyncMock()
        mock_redis.setex.return_value = True
        cache = CacheService(redis=mock_redis)

        result = await cache.set("test_key", "test_value", ttl=60)
        assert result is True
        mock_redis.setex.assert_awaited_once_with("test_key", 60, "test_value")

    @pytest.mark.asyncio
    async def test_delete_returns_true(self) -> None:
        """delete() returns True when key is deleted."""
        mock_redis = AsyncMock()
        mock_redis.delete.return_value = 1
        cache = CacheService(redis=mock_redis)

        result = await cache.delete("test_key")
        assert result is True

    @pytest.mark.asyncio
    async def test_get_or_compute_cache_hit(self) -> None:
        """get_or_compute returns cached value without calling compute_fn."""
        mock_redis = AsyncMock()
        mock_redis.get.return_value = "cached"
        cache = CacheService(redis=mock_redis)
        compute_fn = MagicMock()

        result = await cache.get_or_compute("key", compute_fn)
        assert result == "cached"
        compute_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_or_compute_cache_miss(self) -> None:
        """get_or_compute calls compute_fn on cache miss."""
        mock_redis = AsyncMock()
        # First get returns None (miss), then setex succeeds
        mock_redis.get.side_effect = [None, None]
        mock_redis.set.return_value = True  # stampede lock
        mock_redis.setex.return_value = True
        cache = CacheService(redis=mock_redis)

        result = await cache.get_or_compute("key", lambda: "computed_value")
        assert result == "computed_value"

    def test_build_context_cache_key_is_deterministic(self) -> None:
        """Same inputs produce the same cache key."""
        key1 = CacheService.build_context_cache_key("org1", "user1", "hello world")
        key2 = CacheService.build_context_cache_key("org1", "user1", "hello world")
        assert key1 == key2
        assert key1.startswith("ctx:")

    def test_build_context_cache_key_differs_for_diff_inputs(self) -> None:
        """Different inputs produce different cache keys."""
        key1 = CacheService.build_context_cache_key("org1", "user1", "query one")
        key2 = CacheService.build_context_cache_key("org1", "user1", "query two")
        assert key1 != key2

    def test_build_user_cache_pattern(self) -> None:
        """Pattern ends with * for SCAN matching."""
        pattern = CacheService.build_user_cache_pattern("org1", "user1")
        assert pattern == "ctx:org1:user1:*"
