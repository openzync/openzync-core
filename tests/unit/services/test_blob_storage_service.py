"""Unit tests for BlobStorageService — blob upload, delete, and validation.

All external dependencies (S3 BlobStorage, DB repository) are mocked.
FastAPI's ``UploadFile`` is replaced with a lightweight MagicMock.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import UploadFile

from core.blob_storage import S3StorageError
from core.exceptions import PayloadTooLargeError, ValidationError
from services.blob_storage_service import BlobStorageService

from schemas.memory import BlobMetadata


@pytest.mark.unit
class TestBlobStorageService:
    """Unit tests for ``BlobStorageService`` — upload orchestration and validation."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
    EPISODE_ID = UUID("00000000-0000-0000-0000-000000000003")
    SESSION_ID = UUID("00000000-0000-0000-0000-000000000004")
    USER_ID = UUID("00000000-0000-0000-0000-000000000005")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(self) -> tuple[BlobStorageService, AsyncMock, AsyncMock]:
        """Create a BlobStorageService with mocked DB + blob repo."""
        mock_db = AsyncMock()
        mock_blob_repo = AsyncMock()
        service = BlobStorageService(db=mock_db, blob_repo=mock_blob_repo)
        return service, mock_db, mock_blob_repo

    def _make_upload_file(
        self, filename: str = "test.pdf", content: bytes = b"hello world",
        content_type: str = "application/pdf",
    ) -> MagicMock:
        """Build a MagicMock mimicking FastAPI's UploadFile."""
        upload_file = MagicMock(spec=UploadFile)
        upload_file.filename = filename
        upload_file.content_type = content_type
        upload_file.read = AsyncMock(return_value=content)
        return upload_file

    def _make_storage_config(self, **overrides: dict) -> dict:
        """Build a minimal blob storage config dict."""
        config = {
            "backend": "s3",
            "endpoint_url": "http://minio:9000",
            "region": "auto",
            "access_key_id": "minioadmin",
            "secret_access_key": "minioadmin",
            "bucket_name": "test-blobs",
            "max_blob_size_mb": 50,
        }
        config.update(overrides)
        return config

    # ── upload_blobs ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_upload_blobs_success(self) -> None:
        """``upload_blobs`` uploads to S3 and batch-creates DB records."""
        service, _mock_db, mock_blob_repo = self._make_service()

        upload_file = self._make_upload_file()

        mock_blob = MagicMock()
        mock_blob.id = uuid4()
        mock_blob.file_name = "test.pdf"
        mock_blob_repo.batch_create.return_value = [mock_blob]

        with patch(
            "services.blob_storage_service.BlobStorage",
        ) as mock_storage_cls:
            mock_storage = AsyncMock()
            mock_storage_cls.return_value = mock_storage

            result = await service.upload_blobs(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                episode_id=self.EPISODE_ID,
                session_id=self.SESSION_ID,
                created_by=self.USER_ID,
                uploaded_files=[upload_file],
                blob_metadatas=[
                    BlobMetadata(blob_id=0, mime_type="application/pdf", file_name="test.pdf"),
                ],
                storage_config=self._make_storage_config(),
            )

        assert len(result) == 1
        assert result[0].id == mock_blob.id
        # BlobStorage.upload was called
        mock_storage.upload.assert_awaited_once()
        mock_blob_repo.batch_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upload_too_many_blobs_raises_validation_error(self) -> None:
        """``upload_blobs`` raises ValidationError when exceeding MAX_BLOBS_PER_REQUEST."""
        service, _mock_db, _mock_blob_repo = self._make_service()

        # Create more blobs than the limit (default 50)
        metadatas = [
            BlobMetadata(blob_id=i, mime_type="text/plain", file_name=f"f{i}.txt")
            for i in range(51)
        ]

        with pytest.raises(ValidationError, match="Too many blobs"):
            await service.upload_blobs(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                episode_id=self.EPISODE_ID,
                session_id=self.SESSION_ID,
                created_by=self.USER_ID,
                uploaded_files=[],
                blob_metadatas=metadatas,
                storage_config=self._make_storage_config(),
            )

    @pytest.mark.asyncio
    async def test_upload_blob_id_out_of_range_raises_validation_error(self) -> None:
        """``upload_blobs`` raises ValidationError when blob_id indexes beyond uploaded_files."""
        service, _mock_db, _mock_blob_repo = self._make_service()

        upload_file = self._make_upload_file()

        with pytest.raises(ValidationError, match="blob_id.*out of range"):
            await service.upload_blobs(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                episode_id=self.EPISODE_ID,
                session_id=self.SESSION_ID,
                created_by=self.USER_ID,
                uploaded_files=[upload_file],  # only 1 file
                blob_metadatas=[
                    BlobMetadata(blob_id=3, mime_type="text/plain", file_name="ghost.txt"),
                ],
                storage_config=self._make_storage_config(),
            )

    @pytest.mark.asyncio
    async def test_upload_payload_too_large_raises_error(self) -> None:
        """``upload_blobs`` raises PayloadTooLargeError when blob exceeds size limit."""
        service, _mock_db, _mock_blob_repo = self._make_service()

        # Create a 60MB blob (exceeds 50MB limit)
        big_content = b"x" * (51 * 1024 * 1024)
        upload_file = self._make_upload_file(content=big_content)

        with pytest.raises(PayloadTooLargeError, match="exceeds limit"):
            await service.upload_blobs(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                episode_id=self.EPISODE_ID,
                session_id=self.SESSION_ID,
                created_by=self.USER_ID,
                uploaded_files=[upload_file],
                blob_metadatas=[
                    BlobMetadata(blob_id=0, mime_type="application/pdf", file_name="big.pdf"),
                ],
                storage_config=self._make_storage_config(),
            )

    @pytest.mark.asyncio
    async def test_upload_empty_blobs_returns_empty_list(self) -> None:
        """``upload_blobs`` returns empty list when no blob metadatas are provided."""
        service, _mock_db, mock_blob_repo = self._make_service()

        with patch("services.blob_storage_service.BlobStorage"):
            result = await service.upload_blobs(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                episode_id=self.EPISODE_ID,
                session_id=self.SESSION_ID,
                created_by=self.USER_ID,
                uploaded_files=[],
                blob_metadatas=[],
                storage_config=self._make_storage_config(),
            )

        assert result == []
        mock_blob_repo.batch_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_upload_fallback_mime_type(self) -> None:
        """``upload_blobs`` falls back to octet-stream when MIME type is missing."""
        service, _mock_db, mock_blob_repo = self._make_service()

        upload_file = self._make_upload_file(content_type=None)
        mock_blob_repo.batch_create.return_value = [MagicMock(id=uuid4())]

        with patch(
            "services.blob_storage_service.BlobStorage",
        ) as mock_storage_cls:
            mock_storage = AsyncMock()
            mock_storage_cls.return_value = mock_storage

            result = await service.upload_blobs(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                episode_id=self.EPISODE_ID,
                session_id=self.SESSION_ID,
                created_by=self.USER_ID,
                uploaded_files=[upload_file],
                blob_metadatas=[
                    BlobMetadata(blob_id=0, mime_type="application/octet-stream", file_name="file.bin"),
                ],
                storage_config=self._make_storage_config(),
            )

        assert len(result) == 1
        mock_storage.upload.assert_awaited_once()

    # ── delete_blobs ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_delete_blobs_deletes_from_storage(self) -> None:
        """``delete_blobs`` calls storage.delete for each blob record."""
        service, _mock_db, _mock_blob_repo = self._make_service()

        blob_record = MagicMock()
        blob_record.id = uuid4()
        blob_record.storage_key = "org/proj/ep/blobs/0/test.pdf"

        with patch(
            "services.blob_storage_service.BlobStorage",
        ) as mock_storage_cls:
            mock_storage = AsyncMock()
            mock_storage_cls.return_value = mock_storage

            await service.delete_blobs(
                blob_records=[blob_record],
                storage_config=self._make_storage_config(),
            )

        mock_storage.delete.assert_awaited_once_with(blob_record.storage_key)

    @pytest.mark.asyncio
    async def test_delete_blobs_empty_is_noop(self) -> None:
        """``delete_blobs`` is a no-op when blob_records is empty."""
        service, _mock_db, _mock_blob_repo = self._make_service()

        with patch("services.blob_storage_service.BlobStorage") as mock_storage_cls:
            await service.delete_blobs(
                blob_records=[],
                storage_config=self._make_storage_config(),
            )

        mock_storage_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_blobs_s3_error_logged_not_raised(self) -> None:
        """``delete_blobs`` logs S3 errors but does not raise."""
        service, _mock_db, _mock_blob_repo = self._make_service()

        blob_record = MagicMock()
        blob_record.id = uuid4()
        blob_record.storage_key = "some/key"

        with patch(
            "services.blob_storage_service.BlobStorage",
        ) as mock_storage_cls:
            mock_storage = AsyncMock()
            mock_storage.delete.side_effect = S3StorageError("S3 error")
            mock_storage_cls.return_value = mock_storage

            # Should not raise
            await service.delete_blobs(
                blob_records=[blob_record],
                storage_config=self._make_storage_config(),
            )

    # ── validate_blob_metadata_count ────────────────────────────────────────

    def test_validate_blob_metadata_count_under_limit(self) -> None:
        """``validate_blob_metadata_count`` passes when count is under limit."""
        metadatas = [
            BlobMetadata(blob_id=i, mime_type="text/plain", file_name=f"f{i}.txt")
            for i in range(5)
        ]
        # Should not raise
        BlobStorageService.validate_blob_metadata_count(metadatas)

    def test_validate_blob_metadata_count_over_limit(self) -> None:
        """``validate_blob_metadata_count`` raises ValidationError when over limit."""
        metadatas = [
            BlobMetadata(blob_id=i, mime_type="text/plain", file_name=f"f{i}.txt")
            for i in range(11)  # MAX_BLOBS_PER_MESSAGE default = 10
        ]
        with pytest.raises(ValidationError, match="Too many blobs per message"):
            BlobStorageService.validate_blob_metadata_count(metadatas)

    # ── generate_download_url ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_generate_download_url_returns_url(self) -> None:
        """``generate_download_url`` returns a presigned URL on success."""
        with patch(
            "services.blob_storage_service.BlobStorage",
        ) as mock_storage_cls:
            mock_storage = AsyncMock()
            mock_storage.get_presigned_url.return_value = "https://minio/test-bucket/key?X-Amz=..."
            mock_storage_cls.return_value = mock_storage

            url = await BlobStorageService.generate_download_url(
                storage_key="some/key",
                storage_config=self._make_storage_config(),
                expires_in=300,
            )

        assert url == "https://minio/test-bucket/key?X-Amz=..."

    @pytest.mark.asyncio
    async def test_generate_download_url_returns_none_on_failure(self) -> None:
        """``generate_download_url`` returns None on S3 error."""
        with patch(
            "services.blob_storage_service.BlobStorage",
        ) as mock_storage_cls:
            mock_storage = AsyncMock()
            mock_storage.get_presigned_url.side_effect = S3StorageError("S3 unreachable")
            mock_storage_cls.return_value = mock_storage

            url = await BlobStorageService.generate_download_url(
                storage_key="some/key",
                storage_config=self._make_storage_config(),
            )

        assert url is None
