"""Regression: blob delete concurrency bound + fail-loud (item 6).

- ``delete_blobs`` caps in-flight S3 deletes with ``Semaphore(10)``.
- An S3 failure propagates as ``S3StorageError`` (no silent swallow).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.blob_storage import S3StorageError
from services.blob_storage_service import BlobStorageService


def _make_service() -> BlobStorageService:
    return BlobStorageService(db=AsyncMock(), blob_repo=AsyncMock())


def _make_config() -> dict:
    return {
        "backend": "s3",
        "endpoint_url": "http://minio:9000",
        "region": "auto",
        "access_key_id": "minioadmin",
        "secret_access_key": "minioadmin",
        "bucket_name": "test-blobs",
        "max_blob_size_mb": 50,
    }


def test_delete_semaphore_bound_is_ten() -> None:
    """The delete fan-out is bounded by ``Semaphore(10)`` in source."""
    src = (
        Path(__file__).resolve().parents[3] / "services" / "blob_storage_service.py"
    ).read_text()
    assert "asyncio.Semaphore(10)" in src


@pytest.mark.asyncio
async def test_delete_blobs_bounds_concurrency_to_ten() -> None:
    """25 blobs delete with at most 10 S3 deletes in flight."""
    service = _make_service()
    blobs = [
        MagicMock(id=uuid4(), storage_key=f"org/proj/ep/blobs/{i}/f{i}.pdf")
        for i in range(25)
    ]
    in_flight = 0
    max_in_flight = 0

    async def _tracked_delete(key: str) -> None:  # noqa: ANN202
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0)  # yield so gathers actually overlap
        in_flight -= 1

    with patch("services.blob_storage_service.BlobStorage") as mock_cls:
        mock_cls.return_value.delete = AsyncMock(side_effect=_tracked_delete)
        await service.delete_blobs(blobs, _make_config())

    assert mock_cls.return_value.delete.await_count == 25
    assert max_in_flight <= 10
    assert max_in_flight > 1  # genuinely concurrent, not sequential


@pytest.mark.asyncio
async def test_delete_blobs_s3_error_propagates() -> None:
    """An S3 delete failure raises ``S3StorageError`` to the caller."""
    service = _make_service()
    blobs = [MagicMock(id=uuid4(), storage_key="org/proj/ep/blobs/0/f.pdf")]
    with patch("services.blob_storage_service.BlobStorage") as mock_cls:
        mock_cls.return_value.delete = AsyncMock(
            side_effect=S3StorageError("S3 delete failed")
        )
        with pytest.raises(S3StorageError, match="S3 delete failed"):
            await service.delete_blobs(blobs, _make_config())
