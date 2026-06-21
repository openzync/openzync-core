"""Unit tests for IdempotencyService — 3-layer idempotency with mocked Redis."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from services.idempotency_service import (
    IdempotencyService,
    IdempotencyStatus,
)


@pytest.mark.unit
class TestIdempotencyService:
    """IdempotencyService unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")

    @pytest.fixture
    def mock_redis(self) -> AsyncMock:
        redis = AsyncMock()
        redis.get.return_value = None
        return redis

    @pytest.mark.asyncio
    async def test_check_idempotency_key_new(self, mock_redis: AsyncMock) -> None:
        """A new key returns NEW status."""
        mock_redis.get.return_value = None
        service = IdempotencyService(redis=mock_redis)

        result = await service.check_idempotency_key("new-key", "hash123")
        assert result.status == IdempotencyStatus.NEW
        assert result.response_data is None

    @pytest.mark.asyncio
    async def test_check_idempotency_key_replay(self, mock_redis: AsyncMock) -> None:
        """An existing key with matching hash returns REPLAY."""
        import orjson

        cached = orjson.dumps(
            {
                "request_body_hash": "hash123",
                "response_body": {"job_id": "job-123", "episode_count": 2},
            }
        )
        mock_redis.get.return_value = cached
        service = IdempotencyService(redis=mock_redis)

        result = await service.check_idempotency_key("existing-key", "hash123")
        assert result.status == IdempotencyStatus.REPLAY
        assert result.response_data is not None

    @pytest.mark.asyncio
    async def test_check_idempotency_key_conflict(self, mock_redis: AsyncMock) -> None:
        """An existing key with different hash returns CONFLICT."""
        import orjson

        cached = orjson.dumps(
            {
                "request_body_hash": "original-hash",
                "response_body": {"job_id": "job-123"},
            }
        )
        mock_redis.get.return_value = cached
        service = IdempotencyService(redis=mock_redis)

        result = await service.check_idempotency_key(
            "existing-key", "different-hash"
        )
        assert result.status == IdempotencyStatus.CONFLICT

    @pytest.mark.asyncio
    async def test_store_idempotency_key(self, mock_redis: AsyncMock) -> None:
        """Storing a key calls setex on Redis."""
        mock_redis.setex.return_value = True
        service = IdempotencyService(redis=mock_redis)

        await service.store_idempotency_key(
            "new-key", "hash123", {"job_id": "job-456"}
        )
        mock_redis.setex.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_content_hash_detects_duplicate(
        self, mock_redis: AsyncMock
    ) -> None:
        """check_content_hash returns True when hash exists."""
        mock_redis.exists.return_value = True
        service = IdempotencyService(redis=mock_redis)

        result = await service.check_content_hash(
            "org1", "user1", "session1",
            [{"role": "user", "content": "Hello"}],
        )
        assert result is True
        mock_redis.exists.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_content_hash_new(self, mock_redis: AsyncMock) -> None:
        """check_content_hash returns False when hash is new."""
        mock_redis.exists.return_value = False
        service = IdempotencyService(redis=mock_redis)

        result = await service.check_content_hash(
            "org1", "user1", "session1",
            [{"role": "user", "content": "Hello"}],
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_store_content_hash(self, mock_redis: AsyncMock) -> None:
        """store_content_hash returns the content hash string."""
        mock_redis.set.return_value = True
        service = IdempotencyService(redis=mock_redis)

        result = await service.store_content_hash(
            "org1", "user1", "session1",
            [{"role": "user", "content": "Hello"}],
        )
        assert isinstance(result, str)
        assert len(result) > 0
