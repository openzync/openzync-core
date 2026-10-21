"""Async S3-compatible blob storage for episode attachments.

Uses aioboto3 for async S3 operations.  Instantiated per-org from
org config (``OrgConfigBase.to_blob_storage_config()``).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from core.exceptions import ExternalServiceError, PayloadTooLargeError

if TYPE_CHECKING:
    import aioboto3

logger = structlog.get_logger(__name__)

_MIN_MULTIPART_PART_BYTES = 5 * 1024 * 1024
"""Minimum S3 multipart part size (5 MiB) — smaller parts are buffered."""

_MAX_SAFE_FILE_NAME_CHARS = 128
"""Maximum characters kept from a sanitized file name in an S3 key."""


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

    async def upload_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        mime_type: str,
    ) -> str:
        """Upload a blob to S3 from an async chunk iterator (multipart).

        Streams part-by-part so peak memory is one part, not the whole
        file.  Parts smaller than 5 MiB are buffered until the minimum
        S3 part size is reached (except the final part).

        Args:
            key: S3 object key.
            chunks: Async iterator yielding ``bytes`` chunks.
            mime_type: MIME type of the file.

        Returns:
            The S3 object key (same as *key*, confirmed written).

        Raises:
            S3StorageError: If the upload fails (multipart is aborted).
        """
        session = await self._get_session()
        try:
            async with session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
            ) as s3:
                created = await s3.create_multipart_upload(
                    Bucket=self._config.bucket_name,
                    Key=key,
                    ContentType=mime_type,
                )
                upload_id = created["UploadId"]
                try:
                    parts: list[dict[str, Any]] = []
                    part_number = 1
                    pending = bytearray()
                    total = 0
                    async for chunk in chunks:
                        pending.extend(chunk)
                        total += len(chunk)
                        while len(pending) >= _MIN_MULTIPART_PART_BYTES:
                            part = bytes(pending[:_MIN_MULTIPART_PART_BYTES])
                            del pending[:_MIN_MULTIPART_PART_BYTES]
                            resp = await s3.upload_part(
                                Bucket=self._config.bucket_name,
                                Key=key,
                                PartNumber=part_number,
                                UploadId=upload_id,
                                Body=part,
                            )
                            parts.append(
                                {
                                    "ETag": resp["ETag"],
                                    "PartNumber": part_number,
                                }
                            )
                            part_number += 1
                    if pending or not parts:
                        resp = await s3.upload_part(
                            Bucket=self._config.bucket_name,
                            Key=key,
                            PartNumber=part_number,
                            UploadId=upload_id,
                            Body=bytes(pending),
                        )
                        parts.append(
                            {
                                "ETag": resp["ETag"],
                                "PartNumber": part_number,
                            }
                        )
                    await s3.complete_multipart_upload(
                        Bucket=self._config.bucket_name,
                        Key=key,
                        UploadId=upload_id,
                        MultipartUpload={"Parts": parts},
                    )
                except Exception:
                    await s3.abort_multipart_upload(
                        Bucket=self._config.bucket_name,
                        Key=key,
                        UploadId=upload_id,
                    )
                    raise
            logger.info(
                "blob_storage.upload_stream_success",
                key=key,
                bucket=self._config.bucket_name,
                size=total,
                mime_type=mime_type,
            )
            return key
        except (S3StorageError, PayloadTooLargeError):
            # Size-cap violations from the chunk source are caller errors
            # (HTTP 413), not S3 failures — pass through unwrapped so the
            # service layer keeps its status-code mapping.
            raise
        except Exception as exc:
            logger.error(
                "blob_storage.upload_stream_failed",
                key=key,
                bucket=self._config.bucket_name,
                error=str(exc),
            )
            raise S3StorageError(f"Failed to stream-upload blob: {exc}") from exc

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
        expires_in: int = 300,
    ) -> str:
        """Generate a presigned download URL for a blob.

        Args:
            key: S3 object key.
            expires_in: URL expiry in seconds (default 5 minutes).

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
                    Params={
                        "Bucket": self._config.bucket_name,
                        "Key": key,
                        # WHY: attachment is the sole inline-render/XSS
                        # control for client-claimed MIME — never remove it
                        # or add inline serving.
                        "ResponseContentDisposition": "attachment",
                    },
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
    def sanitize_file_name(file_name: str) -> str:
        r"""Sanitize a caller-supplied file name for use in an S3 key.

        Strips NUL bytes (``\\x00`` and ``%00`` — the latter decodes to NUL
        downstream and would otherwise survive allow-listing as ``_00``),
        drops directory components, allow-lists ``[a-zA-Z0-9._-]``
        (everything else → ``_``), and caps the result at 128 characters.

        Args:
            file_name: Raw caller-supplied file name.

        Returns:
            A safe name (``"unnamed"`` when nothing usable remains).
        """
        cleaned = file_name.replace("\x00", "").replace("%00", "")
        base = re.split(r"[/\\]", cleaned)[-1].strip()
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", base)[:_MAX_SAFE_FILE_NAME_CHARS]
        return safe.strip("._") or "unnamed"

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
        safe_name = BlobStorage.sanitize_file_name(file_name)
        return f"blobs/{org_id}/{project_id}/{episode_id}/{blob_index}_{safe_name}"
