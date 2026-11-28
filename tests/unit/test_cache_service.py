"""Unit tests for CacheService — cache-aside with stampede prevention.

Mocks the async Redis client to test logic without infrastructure.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import orjson
import pytest

from core.exceptions import CacheUnavailableError
from services.cache_service import CacheService


@pytest.mark.unit
class TestCacheService:
    """CacheService unit tests."""

    @pytest.mark.asyncio
    async def test_no_redis_raises_value_error(self) -> None:
        """With redis=None, constructor raises ValueError."""
        with pytest.raises(ValueError, match="redis client is required"):
            CacheService(redis=None, default_ttl=60)

    @pytest.mark.asyncio
    async def test_get_returns_cached_value(self) -> None:
        """get() returns the cached string value."""
        mock_redis = AsyncMock()
        mock_redis.get.return_value = "cached_value"
        cache = CacheService(redis=mock_redis, default_ttl=60)

        result = await cache.get("test_key")
        assert result == "cached_value"
        mock_redis.get.assert_awaited_once_with("test_key")

    @pytest.mark.asyncio
    async def test_get_returns_none_on_miss(self) -> None:
        """get() returns None when key does not exist."""
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        cache = CacheService(redis=mock_redis, default_ttl=60)

        result = await cache.get("missing_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_raises_on_redis_error(self) -> None:
        """get() raises CacheUnavailableError when Redis raises."""
        mock_redis = AsyncMock()
        mock_redis.get.side_effect = ConnectionError("Redis down")
        cache = CacheService(redis=mock_redis, default_ttl=60)

        with pytest.raises(CacheUnavailableError, match="Cache read failed"):
            await cache.get("test_key")

    @pytest.mark.asyncio
    async def test_set_stores_with_ttl(self) -> None:
        """set() calls setex with the correct TTL."""
        mock_redis = AsyncMock()
        mock_redis.setex.return_value = True
        cache = CacheService(redis=mock_redis, default_ttl=60)

        result = await cache.set("test_key", "test_value", ttl=60)
        assert result is True
        mock_redis.setex.assert_awaited_once_with("test_key", 60, "test_value")

    @pytest.mark.asyncio
    async def test_delete_returns_true(self) -> None:
        """delete() returns True when key is deleted."""
        mock_redis = AsyncMock()
        mock_redis.delete.return_value = 1
        cache = CacheService(redis=mock_redis, default_ttl=60)

        result = await cache.delete("test_key")
        assert result is True

    @pytest.mark.asyncio
    async def test_no_default_ttl_raises_value_error(self) -> None:
        """With default_ttl=None, constructor raises ValueError."""
        with pytest.raises(ValueError, match="default_ttl is required"):
            CacheService(redis=AsyncMock(), default_ttl=None)

    # ── set() ────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_set_uses_default_ttl_when_none_provided(self) -> None:
        """set() uses self._default_ttl when ttl is not provided."""
        mock_redis = AsyncMock()
        mock_redis.setex.return_value = True
        cache = CacheService(redis=mock_redis, default_ttl=60)

        result = await cache.set("test_key", "test_value")
        assert result is True
        mock_redis.setex.assert_awaited_once_with("test_key", 60, "test_value")

    @pytest.mark.asyncio
    async def test_set_raises_on_redis_error(self) -> None:
        """set() raises CacheUnavailableError when setex raises."""
        mock_redis = AsyncMock()
        mock_redis.setex.side_effect = ConnectionError("Redis down")
        cache = CacheService(redis=mock_redis, default_ttl=60)

        with pytest.raises(CacheUnavailableError, match="Cache write failed"):
            await cache.set("test_key", "test_value")

    # ── delete() ─────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self) -> None:
        """delete() returns False when Redis returns 0."""
        mock_redis = AsyncMock()
        mock_redis.delete.return_value = 0
        cache = CacheService(redis=mock_redis, default_ttl=60)

        result = await cache.delete("test_key")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_raises_on_redis_error(self) -> None:
        """delete() raises CacheUnavailableError on Redis error."""
        mock_redis = AsyncMock()
        mock_redis.delete.side_effect = ConnectionError("Redis down")
        cache = CacheService(redis=mock_redis, default_ttl=60)

        with pytest.raises(CacheUnavailableError, match="Cache delete failed"):
            await cache.delete("test_key")


    def test_build_context_cache_key_uses_sha256_prefix(self) -> None:
        """Key format is ctx:{org}:{proj}:v{ver}:{format}:{limit}:{16-char-hex}."""
        key = CacheService.build_context_cache_key("org1", "proj1", "hello world")
        expected_hash = hashlib.sha256(b"hello world").hexdigest()[:16]
        assert key == f"ctx:org1:proj1:v0:text:20:{expected_hash}"

    def test_build_project_cache_pattern(self) -> None:
        """Returns ctx:{user_id}:{project_id}:* for SCAN matching."""
        pattern = CacheService.build_project_cache_pattern("user1", "proj1")
        assert pattern == "ctx:user1:proj1:*"

    # ── invalidate_project_context() ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_invalidate_project_context_success(self) -> None:
        """Project-scoped SCAN returns keys — delete is called and count returned."""
        mock_redis = AsyncMock()
        mock_redis.scan.return_value = (0, ["ctx:org1:proj1:abc123"])
        mock_redis.delete.return_value = 1
        cache = CacheService(redis=mock_redis, default_ttl=60)

        result = await cache.invalidate_project_context("org1", "proj1")

        assert result == 1
        mock_redis.scan.assert_awaited_once()
        mock_redis.delete.assert_awaited_once_with("ctx:org1:proj1:abc123")

    @pytest.mark.asyncio
    async def test_invalidate_project_context_no_keys(self) -> None:
        """Project-scoped SCAN returns empty — delete not called, count is 0."""
        mock_redis = AsyncMock()
        mock_redis.scan.return_value = (0, [])
        cache = CacheService(redis=mock_redis, default_ttl=60)

        result = await cache.invalidate_project_context("org1", "proj1")

        assert result == 0
        mock_redis.scan.assert_awaited_once()
        mock_redis.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalidate_project_context_raises_on_error(self) -> None:
        """Redis raises during project SCAN — CacheUnavailableError raised."""
        mock_redis = AsyncMock()
        mock_redis.scan.side_effect = ConnectionError("Redis down")
        cache = CacheService(redis=mock_redis, default_ttl=60)

        with pytest.raises(CacheUnavailableError, match="Cache invalidation failed"):
            await cache.invalidate_project_context("org1", "proj1")

    # ── Existing tests kept below (do not remove) ────────────────────────────

    def test_build_context_cache_key_is_deterministic(self) -> None:
        """Same inputs produce the same cache key."""
        key1 = CacheService.build_context_cache_key("org1", "user1", "hello world")
        key2 = CacheService.build_context_cache_key("org1", "user1", "hello world")
        assert key1 == key2
        assert key1.startswith("ctx:")

    def test_build_context_cache_key_differs_for_diff_inputs(self) -> None:
        """Different inputs produce different cache keys."""
        key1 = CacheService.build_context_cache_key("user1", "proj1", "query one")
        key2 = CacheService.build_context_cache_key("user1", "proj1", "query two")
        assert key1 != key2

