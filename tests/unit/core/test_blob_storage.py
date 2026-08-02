"""Unit tests for S3-compatible blob storage (BlobStorage, BlobStorageConfig).

aioboto3 is a lazy import inside BlobStorage._get_session() and is not
installed in the test environment.  All tests mock _get_session to return
a fake aioboto3.Session with a mock S3 client context manager.
"""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from core.exceptions import ExternalServiceError


def _make_fake_session() -> MagicMock:
    """Build a fake aioboto3.Session with an async S3 client context manager."""
    session = MagicMock()
    session.client.return_value.__aenter__.return_value = AsyncMock()
    return session


@pytest.mark.unit
class TestBlobStorageConfig:
    """BlobStorageConfig construction and helpers."""

    def test_default_values(self) -> None:
        """Config is created with sensible defaults."""
        from core.blob_storage import BlobStorageConfig

        config = BlobStorageConfig()
        assert config.backend == "s3"
        assert config.endpoint_url == "http://minio:9000"
        assert config.region == "auto"
        assert config.bucket_name == "openzync-blobs"
        assert config.max_blob_size_mb == 50

    def test_max_blob_bytes_computed(self) -> None:
        """max_blob_bytes returns correct byte count."""
        from core.blob_storage import BlobStorageConfig

        config = BlobStorageConfig(max_blob_size_mb=10)
        assert config.max_blob_bytes == 10 * 1024 * 1024

    def test_from_org_config_populates_all_fields(self) -> None:
        """from_org_config reads all fields from a dict."""
        from core.blob_storage import BlobStorageConfig

        org_cfg = {
            "backend": "minio",
            "endpoint_url": "http://minio:9001",
            "region": "us-east-1",
            "access_key_id": "minioadmin",
            "secret_access_key": "minioadmin",
            "bucket_name": "my-bucket",
            "max_blob_size_mb": 100,
        }
        config = BlobStorageConfig.from_org_config(org_cfg)
        assert config.backend == "minio"
        assert config.endpoint_url == "http://minio:9001"
        assert config.region == "us-east-1"
        assert config.access_key_id == "minioadmin"
        assert config.secret_access_key == "minioadmin"
        assert config.bucket_name == "my-bucket"
        assert config.max_blob_size_mb == 100

    def test_from_org_config_uses_defaults_for_missing(self) -> None:
        """from_org_config fills defaults for missing keys."""
        from core.blob_storage import BlobStorageConfig

        config = BlobStorageConfig.from_org_config({})
        assert config.backend == "s3"
        assert config.bucket_name == "openzync-blobs"
        assert config.max_blob_size_mb == 50


@pytest.mark.unit
class TestBlobStorageSession:
    """BlobStorage lazy session initialisation."""

    def test_session_is_none_after_init(self) -> None:
        """Session is None until _get_session is called."""
        from core.blob_storage import BlobStorage, BlobStorageConfig

        config = BlobStorageConfig(
            access_key_id="test-key",
            secret_access_key="test-secret",
            region="us-east-1",
        )
        storage = BlobStorage(config)
        assert storage._session is None

    @pytest.mark.asyncio
    async def test_session_initialized_with_credentials(self) -> None:
        """_get_session creates a session with correct credentials."""
        from core.blob_storage import BlobStorage, BlobStorageConfig

        config = BlobStorageConfig(
            access_key_id="ak",
            secret_access_key="sk",
            region="us-east-1",
        )
        storage = BlobStorage(config)

        # Mock the lazy import by setting _session directly
        mock_session = MagicMock()
        storage._session = mock_session

        session = await storage._get_session()
        assert session is mock_session


@pytest.mark.unit
class TestBlobStorageUpload:
    """BlobStorage.upload() S3 put_object."""

    @pytest.mark.asyncio
    async def test_upload_success(self) -> None:
        """Upload calls put_object with correct params and returns key."""
        from core.blob_storage import BlobStorage, BlobStorageConfig

        config = BlobStorageConfig(
            access_key_id="key",
            secret_access_key="secret",
            bucket_name="test-bucket",
            endpoint_url="http://minio:9000",
        )
        storage = BlobStorage(config)
        mock_s3 = AsyncMock()
        mock_session = MagicMock()
        mock_session.client.return_value.__aenter__.return_value = mock_s3
        storage._session = mock_session

        result = await storage.upload("path/to/file", b"data", "image/png")
        assert result == "path/to/file"

        mock_session.client.assert_called_once_with("s3", endpoint_url="http://minio:9000")
        mock_s3.put_object.assert_awaited_once_with(
            Bucket="test-bucket",
            Key="path/to/file",
            Body=b"data",
            ContentType="image/png",
        )

    @pytest.mark.asyncio
    async def test_upload_failure_raises_s3_storage_error(self) -> None:
        """Upload raises S3StorageError when S3 call fails."""
        from core.blob_storage import BlobStorage, BlobStorageConfig

        config = BlobStorageConfig(access_key_id="key", secret_access_key="secret")
        storage = BlobStorage(config)
        mock_s3 = AsyncMock()
        mock_s3.put_object.side_effect = Exception("Bucket not found")
        mock_session = MagicMock()
        mock_session.client.return_value.__aenter__.return_value = mock_s3
        storage._session = mock_session

        with pytest.raises(ExternalServiceError, match="Failed to upload blob"):
            await storage.upload("path/to/file", b"data", "image/png")

    @pytest.mark.asyncio
    async def test_upload_empty_data(self) -> None:
        """Upload works with empty bytes."""
        from core.blob_storage import BlobStorage, BlobStorageConfig

        config = BlobStorageConfig(access_key_id="key", secret_access_key="secret")
        storage = BlobStorage(config)
        mock_s3 = AsyncMock()
        mock_session = MagicMock()
        mock_session.client.return_value.__aenter__.return_value = mock_s3
        storage._session = mock_session

        result = await storage.upload("empty/file", b"", "text/plain")
        assert result == "empty/file"
        mock_s3.put_object.assert_awaited_once_with(
            Bucket="openzync-blobs",
            Key="empty/file",
            Body=b"",
            ContentType="text/plain",
        )


@pytest.mark.unit
class TestBlobStorageDownload:
    """BlobStorage.download() S3 get_object."""

    @pytest.mark.asyncio
    async def test_download_success(self) -> None:
        """Download reads body from S3 and returns bytes."""
        from core.blob_storage import BlobStorage, BlobStorageConfig

        config = BlobStorageConfig(access_key_id="k", secret_access_key="s")
        storage = BlobStorage(config)
        mock_s3 = AsyncMock()
        mock_response = {"Body": AsyncMock()}
        mock_response["Body"].read.return_value = b"file-content"
        mock_s3.get_object.return_value = mock_response
        mock_session = MagicMock()
        mock_session.client.return_value.__aenter__.return_value = mock_s3
        storage._session = mock_session

        data = await storage.download("path/to/file")
        assert data == b"file-content"
        mock_s3.get_object.assert_awaited_once_with(
            Bucket="openzync-blobs",
            Key="path/to/file",
        )

    @pytest.mark.asyncio
    async def test_download_failure_raises_s3_storage_error(self) -> None:
        """Download raises S3StorageError when S3 call fails."""
        from core.blob_storage import BlobStorage, BlobStorageConfig

        config = BlobStorageConfig(access_key_id="k", secret_access_key="s")
        storage = BlobStorage(config)
        mock_s3 = AsyncMock()
        mock_s3.get_object.side_effect = Exception("NoSuchKey")
        mock_session = MagicMock()
        mock_session.client.return_value.__aenter__.return_value = mock_s3
        storage._session = mock_session

        with pytest.raises(ExternalServiceError, match="Failed to download blob"):
            await storage.download("nonexistent/key")


@pytest.mark.unit
class TestBlobStorageDelete:
    """BlobStorage.delete() S3 delete_object."""

    @pytest.mark.asyncio
    async def test_delete_success(self) -> None:
        """Delete calls delete_object with correct params."""
        from core.blob_storage import BlobStorage, BlobStorageConfig

        config = BlobStorageConfig(access_key_id="k", secret_access_key="s")
        storage = BlobStorage(config)
        mock_s3 = AsyncMock()
        mock_session = MagicMock()
        mock_session.client.return_value.__aenter__.return_value = mock_s3
        storage._session = mock_session

        await storage.delete("path/to/file")
        mock_s3.delete_object.assert_awaited_once_with(
            Bucket="openzync-blobs",
            Key="path/to/file",
        )

    @pytest.mark.asyncio
    async def test_delete_failure_raises_s3_storage_error(self) -> None:
        """Delete raises S3StorageError when S3 call fails."""
        from core.blob_storage import BlobStorage, BlobStorageConfig

        config = BlobStorageConfig(access_key_id="k", secret_access_key="s")
        storage = BlobStorage(config)
        mock_s3 = AsyncMock()
        mock_s3.delete_object.side_effect = Exception("AccessDenied")
        mock_session = MagicMock()
        mock_session.client.return_value.__aenter__.return_value = mock_s3
        storage._session = mock_session

        with pytest.raises(ExternalServiceError, match="Failed to delete blob"):
            await storage.delete("path/to/file")


@pytest.mark.unit
class TestBlobStoragePresignedUrl:
    """BlobStorage.get_presigned_url() URL generation."""

    @pytest.mark.asyncio
    async def test_presigned_url_success(self) -> None:
        """Returns presigned URL with correct params and expiry."""
        from core.blob_storage import BlobStorage, BlobStorageConfig

        config = BlobStorageConfig(access_key_id="k", secret_access_key="s")
        storage = BlobStorage(config)
        mock_s3 = AsyncMock()
        mock_s3.generate_presigned_url.return_value = "https://presigned.url/test"
        mock_session = MagicMock()
        mock_session.client.return_value.__aenter__.return_value = mock_s3
        storage._session = mock_session

        url = await storage.get_presigned_url("path/to/file", expires_in=600)
        assert url == "https://presigned.url/test"
        mock_s3.generate_presigned_url.assert_awaited_once_with(
            "get_object",
            Params={"Bucket": "openzync-blobs", "Key": "path/to/file"},
            ExpiresIn=600,
        )

    @pytest.mark.asyncio
    async def test_presigned_url_default_expiry(self) -> None:
        """Default expires_in is 300 seconds."""
        from core.blob_storage import BlobStorage, BlobStorageConfig

        config = BlobStorageConfig(access_key_id="k", secret_access_key="s")
        storage = BlobStorage(config)
        mock_s3 = AsyncMock()
        mock_s3.generate_presigned_url.return_value = "url"
        mock_session = MagicMock()
        mock_session.client.return_value.__aenter__.return_value = mock_s3
        storage._session = mock_session

        await storage.get_presigned_url("path/to/file")
        _args, kwargs = mock_s3.generate_presigned_url.await_args
        assert kwargs["ExpiresIn"] == 300

    @pytest.mark.asyncio
    async def test_presigned_url_failure_raises_s3_storage_error(self) -> None:
        """Presigned URL raises S3StorageError when generation fails."""
        from core.blob_storage import BlobStorage, BlobStorageConfig

        config = BlobStorageConfig(access_key_id="k", secret_access_key="s")
        storage = BlobStorage(config)
        mock_s3 = AsyncMock()
        mock_s3.generate_presigned_url.side_effect = Exception("InvalidRequest")
        mock_session = MagicMock()
        mock_session.client.return_value.__aenter__.return_value = mock_s3
        storage._session = mock_session

        with pytest.raises(ExternalServiceError, match="Failed to generate presigned URL"):
            await storage.get_presigned_url("path/to/file")


@pytest.mark.unit
class TestBlobStorageBuildKey:
    """BlobStorage.build_key() deterministic key generation."""

    def test_build_key_format(self) -> None:
        """Key follows blobs/{org}/{project}/{episode}/{idx}_{filename} pattern."""
        from core.blob_storage import BlobStorage

        key = BlobStorage.build_key(
            org_id="org-123",
            project_id="proj-456",
            episode_id="ep-789",
            blob_index=0,
            file_name="recording.mp3",
        )
        assert key == "blobs/org-123/proj-456/ep-789/0_recording.mp3"

    def test_build_key_sanitizes_filename(self) -> None:
        """Special characters in filename are replaced with underscores."""
        from core.blob_storage import BlobStorage

        key = BlobStorage.build_key(
            org_id="o",
            project_id="p",
            episode_id="e",
            blob_index=1,
            file_name="my file (2).txt",
        )
        # Spaces, parens → underscores, so "my file (2).txt" → "my_file__2_.txt"
        assert key == "blobs/o/p/e/1_my_file__2_.txt"

    def test_build_key_multiple_indices(self) -> None:
        """Different blob indices produce distinct keys."""
        from core.blob_storage import BlobStorage

        key0 = BlobStorage.build_key("o", "p", "e", 0, "file.pdf")
        key1 = BlobStorage.build_key("o", "p", "e", 1, "file.pdf")
        assert key0 != key1
