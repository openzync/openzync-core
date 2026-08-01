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
        """check_content_hash returns the stored payload when hash exists."""
        mock_redis.get.return_value = "job-123"
        service = IdempotencyService(redis=mock_redis)

        result = await service.check_content_hash(
            "org1", "user1", "session1",
            [{"role": "user", "content": "Hello"}],
        )
        assert result == "job-123"
        mock_redis.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_content_hash_new(self, mock_redis: AsyncMock) -> None:
        """check_content_hash returns None when hash is new."""
        service = IdempotencyService(redis=mock_redis)

        result = await service.check_content_hash(
            "org1", "user1", "session1",
            [{"role": "user", "content": "Hello"}],
        )
        assert result is None
        mock_redis.get.assert_awaited_once()

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

    @pytest.mark.asyncio
    async def test_store_content_hash_with_payload_roundtrip(
        self, mock_redis: AsyncMock,
    ) -> None:
        """Payload stored with SETNX; check returns it unchanged."""
        stored_value: dict = {}

        async def _fake_set(key: str, value: str, **kwargs: object) -> bool:
            stored_value["key"] = key
            stored_value["value"] = value
            return True

        mock_redis.set.side_effect = _fake_set
        service = IdempotencyService(redis=mock_redis)

        content_hash = await service.store_content_hash(
            "org1", "user1", "session1",
            [{"role": "user", "content": "Hello"}],
            payload="job-456",
        )
        assert stored_value["value"] == "job-456"

        mock_redis.get.return_value = stored_value["value"]
        result = await service.check_content_hash(
            "org1", "user1", "session1",
            [{"role": "user", "content": "Hello"}],
        )
        assert result == "job-456"
        assert content_hash == stored_value["key"].rsplit(":", 1)[-1]

    def test_compute_content_hash_includes_metadata_and_blobs(self) -> None:
        """Different metadata/blobs produce different hashes."""
        base = [{"role": "user", "content": "Hello"}]
        with_meta = [
            {"role": "user", "content": "Hello", "metadata": {"tag": "a"}}
        ]
        with_blobs = [
            {
                "role": "user",
                "content": "Hello",
                "blobs": [
                    {"blob_id": 0, "mime_type": "image/png", "file_name": "a.png"}
                ],
            }
        ]

        h_base = IdempotencyService.compute_content_hash("org1", "user1", "s1", base)
        h_meta = IdempotencyService.compute_content_hash(
            "org1", "user1", "s1", with_meta
        )
        h_blobs = IdempotencyService.compute_content_hash(
            "org1", "user1", "s1", with_blobs
        )

        assert h_meta != h_base  # metadata participates in the hash
        assert h_blobs != h_base  # blobs participate in the hash
        assert h_meta != h_blobs

    def test_compute_content_hash_excludes_absent_metadata_blobs(self) -> None:
        """Messages without metadata/blobs keys hash identically regardless."""
        msg_plain = [{"role": "user", "content": "Hello"}]
        # Empty metadata/blobs explicitly present MUST NOT collide with absent.
        msg_explicit_empty = [
            {"role": "user", "content": "Hello", "metadata": {}, "blobs": []}
        ]
        # Absence vs presence changes the shape — no collision is the contract.
        h1 = IdempotencyService.compute_content_hash("org1", "user1", "s1", msg_plain)
        h2 = IdempotencyService.compute_content_hash(
            "org1", "user1", "s1", msg_explicit_empty
        )
        assert h1 != h2

        # Determinism: same dicts hash the same.
        h3 = IdempotencyService.compute_content_hash("org1", "user1", "s1", msg_plain)
        assert h1 == h3
