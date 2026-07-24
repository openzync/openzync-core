"""Async S3-compatible blob storage for episode attachments.

Uses aioboto3 for async S3 operations.  Instantiated per-org from
org config (``OrgConfigBase.to_blob_storage_config()``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from core.exceptions import ExternalServiceError

if TYPE_CHECKING:
    import aioboto3

logger = structlog.get_logger(__name__)


@dataclass
class BlobStorageConfig:
    """Configuration for an S3-compatible blob storage backend."""

    backend: str = "s3"
    endpoint_url: str = "http://minio:9000"
    region: str = "auto"
    access_key_id: str = ""
    secret_access_key: str = ""
    bucket_name: str = "openzync-blobs"
    max_blob_size_mb: int = 50

    @property
    def max_blob_bytes(self) -> int:
        """Return the maximum blob size in bytes."""
        return self.max_blob_size_mb * 1024 * 1024

    @classmethod
    def from_org_config(cls, config: dict[str, Any]) -> BlobStorageConfig:
        """Create config from an org config dict.

        Args:
            config: Dict from ``OrgConfigBase.to_blob_storage_config()``.

        Returns:
            A populated :class:`BlobStorageConfig`.
        """
        return cls(
            backend=config.get("backend", "s3"),
            endpoint_url=config.get("endpoint_url", "http://minio:9000"),
            region=config.get("region", "auto"),
            access_key_id=config.get("access_key_id", ""),
            secret_access_key=config.get("secret_access_key", ""),
            bucket_name=config.get("bucket_name", "openzync-blobs"),
            max_blob_size_mb=config.get("max_blob_size_mb", 50),
        )


class S3StorageError(ExternalServiceError):
    """S3-compatible storage operation failed.

    Inherits from :class:`ExternalServiceError` (HTTP 502) so callers
    get a consistent error response without custom handler registration.
    """

    code: str = "s3_storage_error"

    def __init__(
        self,
        message: str = "An S3 storage operation failed.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class BlobStorage:
    """Async S3-compatible blob storage.

    Usage::

        config = BlobStorageConfig.from_org_config(org_config.to_blob_storage_config())
        storage = BlobStorage(config)
        key = await storage.upload("path/to/file", b"...", "image/png")
        data = await storage.download(key)
    """

    def __init__(self, config: BlobStorageConfig) -> None:
        self._config = config
        self._session: aioboto3.Session | None = None

    async def _get_session(self) -> aioboto3.Session:
        """Lazy-init an aioboto3 session.

        Returns:
            An :class:`aioboto3.Session` configured with the org's credentials.
        """
        if self._session is None:
            import aioboto3  # lazy: optional dependency, only imported when used

            self._session = aioboto3.Session(
                aws_access_key_id=self._config.access_key_id,
                aws_secret_access_key=self._config.secret_access_key,
                region_name=self._config.region,
            )
        return self._session

    async def upload(
        self,
        key: str,
        data: bytes,
        mime_type: str,
    ) -> str:
        """Upload a blob to S3.

        Args:
            key: S3 object key (e.g. ``blobs/{org_id}/{project_id}/{episode_id}/{idx}_{filename}``).
            data: Raw bytes of the file.
            mime_type: MIME type of the file (e.g. ``"image/png"``).

        Returns:
            The S3 object key (same as *key*, confirmed written).

        Raises:
            S3StorageError: If the upload fails.
        """
        session = await self._get_session()
        try:
            async with session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
            ) as s3:
                await s3.put_object(
                    Bucket=self._config.bucket_name,
                    Key=key,
                    Body=data,
                    ContentType=mime_type,
                )
            logger.info(
                "blob_storage.upload_success",
                key=key,
                bucket=self._config.bucket_name,
                size=len(data),
                mime_type=mime_type,
            )
            return key
        except Exception as exc:
            logger.error(
                "blob_storage.upload_failed",
                key=key,
                bucket=self._config.bucket_name,
                error=str(exc),
            )
            raise S3StorageError(f"Failed to upload blob: {exc}") from exc

    async def download(self, key: str) -> bytes:
        """Download a blob from S3.

        Args:
            key: S3 object key.

        Returns:
            Raw bytes of the blob.

        Raises:
            S3StorageError: If the download fails.
        """
        session = await self._get_session()
        try:
            async with session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
            ) as s3:
                response = await s3.get_object(
                    Bucket=self._config.bucket_name,
                    Key=key,
                )
                data = await response["Body"].read()
            logger.info(
                "blob_storage.download_success",
                key=key,
                bucket=self._config.bucket_name,
                size=len(data),
            )
            return data
        except Exception as exc:
            logger.error(
                "blob_storage.download_failed",
                key=key,
                bucket=self._config.bucket_name,
                error=str(exc),
            )
            raise S3StorageError(f"Failed to download blob: {exc}") from exc

    async def delete(self, key: str) -> None:
        """Delete a blob from S3.

        Args:
            key: S3 object key.

        Raises:
            S3StorageError: If the deletion fails.
        """
        session = await self._get_session()
        try:
            async with session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
            ) as s3:
                await s3.delete_object(
                    Bucket=self._config.bucket_name,
                    Key=key,
                )
            logger.info(
                "blob_storage.delete_success",
                key=key,
                bucket=self._config.bucket_name,
            )
        except Exception as exc:
            logger.error(
                "blob_storage.delete_failed",
                key=key,
                bucket=self._config.bucket_name,
                error=str(exc),
            )
            raise S3StorageError(f"Failed to delete blob: {exc}") from exc

    async def get_presigned_url(
        self,
        key: str,
        expires_in: int = 3600,
    ) -> str:
        """Generate a presigned download URL for a blob.

        Args:
            key: S3 object key.
            expires_in: URL expiry in seconds (default 1 hour).

        Returns:
            A presigned HTTPS URL for temporary direct access.

        Raises:
            S3StorageError: If URL generation fails.
        """
        session = await self._get_session()
        try:
            async with session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
            ) as s3:
                url = await s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._config.bucket_name, "Key": key},
                    ExpiresIn=expires_in,
                )
            logger.info(
                "blob_storage.presigned_url_success",
                key=key,
                bucket=self._config.bucket_name,
                expires_in=expires_in,
            )
            return url
        except Exception as exc:
            logger.error(
                "blob_storage.presigned_url_failed",
                key=key,
                bucket=self._config.bucket_name,
                error=str(exc),
            )
            raise S3StorageError(f"Failed to generate presigned URL: {exc}") from exc

    @staticmethod
    def build_key(
        org_id: str,
        project_id: str,
        episode_id: str,
        blob_index: int,
        file_name: str,
    ) -> str:
        """Build a deterministic S3 key for a blob.

        Pattern::

            blobs/{org_id}/{project_id}/{episode_id}/{blob_index}_{sanitized_name}

        Args:
            org_id: The organization UUID.
            project_id: The project UUID.
            episode_id: The episode UUID.
            blob_index: Zero-based index within the episode.
            file_name: Original file name (special chars replaced with ``_``).

        Returns:
            A deterministic S3 object key.
        """
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", file_name)
        return f"blobs/{org_id}/{project_id}/{episode_id}/{blob_index}_{safe_name}"
