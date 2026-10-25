"""Blob storage orchestration — upload, download, delete, text extraction.

Sits between ``MemoryService`` (business logic) and ``BlobStorage`` (S3 client).
Handles org config resolution, blob-key construction, file validation,
and DB record creation.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from fastapi import UploadFile

from core.blob_storage import BlobStorage, BlobStorageConfig, S3StorageError
from core.config import get_settings
from core.exceptions import PayloadTooLargeError, ValidationError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from models.episode_blob import EpisodeBlob
    from repositories.episode_blob_repository import EpisodeBlobRepository
    from schemas.memory import BlobMetadata

logger = structlog.get_logger(__name__)

# Single source of truth for uploadable MIME types — mirrors the dispatch
# table in workers/tasks/extract_blob_text.py::_dispatch_extraction. Types
# the extractor cannot process are rejected at the upload boundary (422)
# instead of becoming a silent no-op extraction job later.
_ALLOWED_UPLOAD_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/octet-stream",
    }
)

_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB

_PRESIGNED_URL_TTL_SEC = 300
"""Cache TTL for presigned download URLs — matches the default expires_in."""

_PRESIGNED_URL_CACHE_MAX = 1024
"""Cap on cached presigned URLs; overflow purges expired entries only."""

_presigned_url_cache: dict[tuple[str, str, str, int], tuple[str, float]] = {}
"""In-memory presigned-URL cache.

Keyed by ``(endpoint, bucket, key, expires_in)`` → ``(url, cached_at)``.
Presigned URLs are pure signer output (no revocation on regenerate), so
caching them for their own TTL is safe — context assembly calls this once
per blob per request and would otherwise re-sign every time.
Per-process only: each worker/replica signs independently (harmless
redundant signer calls, no revocation semantics, 300s signer output only).
"""


def normalize_upload_mime(mime_type: str) -> str:
    """Strip MIME parameters and lowercase for matching and storage.

    Clients may send ``"TEXT/PDF; charset=binary"`` — the allowlist and the
    stored S3 ContentType / DB value must agree on the bare lowercase type.

    Args:
        mime_type: Raw content type from metadata or the upload.

    Returns:
        The bare lowercase media type (e.g. ``application/pdf``).
    """
    return mime_type.split(";")[0].strip().lower()


def is_allowed_upload_mime(mime_type: str) -> bool:
    """Return whether a MIME type is accepted at the blob upload boundary.

    Args:
        mime_type: Normalized (parameter-stripped, lowercase) content type.

    Returns:
        ``True`` if ``extract_blob_text`` supports the type.
    """
    return (
        mime_type in _ALLOWED_UPLOAD_MIME_TYPES
        or mime_type.startswith("text/")
        or mime_type.startswith("image/")
    )


async def read_upload_bounded(
    upload_file: UploadFile | bytes,
    max_bytes: int,
    file_name: str,
) -> bytes:
    """Read an upload in 1 MiB chunks, aborting as soon as the cap is exceeded.

    Accepts pre-read bytes (fast-path) — callers that already consumed the
    one-shot ``UploadFile`` stream pass bytes to avoid a 0-byte second read.

    Args:
        upload_file: Multipart upload to read from, or pre-read bytes.
        max_bytes: Maximum allowed total size in bytes.
        file_name: Original file name, used in the error message.

    Returns:
        The complete file bytes when under *max_bytes*.

    Raises:
        PayloadTooLargeError: Once the running total exceeds *max_bytes* —
            remaining chunks are never read.
    """
    if isinstance(upload_file, bytes):
        if len(upload_file) > max_bytes:
            raise PayloadTooLargeError(
                f"Blob {file_name} exceeds limit of {max_bytes} bytes"
            )
        return upload_file
    buffer = bytearray()
    while chunk := await upload_file.read(_UPLOAD_CHUNK_BYTES):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise PayloadTooLargeError(
                f"Blob {file_name} exceeds limit of {max_bytes} bytes"
            )
    return bytes(buffer)


async def _stream_upload_capped(
    upload_file: UploadFile,
    max_bytes: int,
    file_name: str,
    storage: BlobStorage,
    key: str,
    mime_type: str,
) -> tuple[str, int]:
    """Stream an ``UploadFile`` to S3 with incremental SHA-256 and size cap.

    Peak memory is one part, not the whole file.  Exceeding *max_bytes*
    aborts the multipart upload — no partial object is left behind.

    Args:
        upload_file: Multipart upload to stream from (one-shot stream).
        max_bytes: Maximum allowed total size in bytes.
        file_name: Original file name, used in the error message.
        storage: The S3 storage client.
        key: Destination S3 object key.
        mime_type: Content type for the stored object.

    Returns:
        Tuple of ``(sha256_hex, total_bytes)``.

    Raises:
        PayloadTooLargeError: Once the running total exceeds *max_bytes*.
        S3StorageError: If the S3 upload fails.
    """
    hasher = hashlib.sha256()
    total = 0

    async def _chunks():  # type: ignore[no-untyped-def]
        nonlocal total
        while True:
            chunk = await upload_file.read(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                return
            total += len(chunk)
            if total > max_bytes:
                raise PayloadTooLargeError(
                    f"Blob {file_name} exceeds limit of {max_bytes} bytes"
                )
            hasher.update(chunk)
            yield chunk

    await storage.upload_stream(key=key, chunks=_chunks(), mime_type=mime_type)
    return hasher.hexdigest(), total


class BlobStorageService:
    """Orchestrates blob upload with S3 storage and DB record creation.

    Usage::

        service = BlobStorageService(db, blob_repo)
        blob_records = await service.upload_blobs(
            org_id=...,
            project_id=...,
            episode_id=...,
            session_id=...,
            created_by=...,
            uploaded_files=[UploadFile(...), ...],
            blob_metadatas=[BlobMetadata(...), ...],
            storage_config={...},
        )
    """

    def __init__(
        self,
        db: AsyncSession,
        blob_repo: EpisodeBlobRepository,
    ) -> None:
        self._db = db
        self._blob_repo = blob_repo

    # ── Upload ──────────────────────────────────────────────────────────────

    async def upload_blobs(
        self,
        org_id: UUID,
        project_id: UUID,
        episode_id: UUID,
        session_id: UUID,
        created_by: UUID,
        uploaded_files: Sequence[UploadFile | bytes],
        blob_metadatas: Sequence[BlobMetadata],
        storage_config: dict[str, Any],
    ) -> list[EpisodeBlob]:
        """Upload blobs to S3 and persist metadata in the DB.

        Reads file bytes, hashes them (SHA-256), uploads to S3, then
        batch-inserts metadata records.  If any single upload fails the
        entire operation is aborted — no partial DB records are created.

        Args:
            org_id: Organization UUID.
            project_id: Project UUID.
            episode_id: Episode UUID.
            session_id: Session UUID.
            created_by: User UUID.
            uploaded_files: The ``UploadFile`` objects from the multipart
                request, or pre-read bytes (one entry per file — bytes
                avoid re-reading the one-shot upload stream).
            blob_metadatas: List of dicts with ``blob_id``, ``file_name``,
                ``mime_type`` keys from the request's ``BlobMetadata``
                schemas.  The ``blob_id`` indexes into ``uploaded_files``.
            storage_config: Flattened org storage config from
                ``OrgConfigBase.to_blob_storage_config()``.

        Returns:
            List of created ``EpisodeBlob`` ORM instances.

        Raises:
            ValidationError: If blob metadata references are out of range, the
                total blob count exceeds ``MAX_BLOBS_PER_REQUEST``, or a
                blob's MIME type is not in the allowlist.
            PayloadTooLargeError: If any blob exceeds the size limit.
            S3StorageError: If the S3 upload fails.
        """
        config = BlobStorageConfig.from_org_config(storage_config)
        storage = BlobStorage(config)
        max_bytes = config.max_blob_bytes
        settings = get_settings()

        if len(blob_metadatas) > settings.MAX_BLOBS_PER_REQUEST:
            raise ValidationError(
                f"Too many blobs: {len(blob_metadatas)} exceeds "
                f"limit of {settings.MAX_BLOBS_PER_REQUEST}"
            )

        blob_dicts: list[dict[str, Any]] = []

        for meta in blob_metadatas:
            if meta.blob_id >= len(uploaded_files):
                raise ValidationError(
                    f"blob_id {meta.blob_id} out of range: "
                    f"only {len(uploaded_files)} files uploaded"
                )

            upload_file = uploaded_files[meta.blob_id]
            if isinstance(upload_file, bytes):
                file_name = meta.file_name or "unnamed"
                fallback_mime = "application/octet-stream"
            else:
                file_name = meta.file_name or upload_file.filename or "unnamed"
                fallback_mime = upload_file.content_type or "application/octet-stream"
            # Single normalized value — drives the allowlist check, the S3
            # ContentType, and the DB record so they can never disagree.
            mime_type = normalize_upload_mime(meta.mime_type or fallback_mime)
            # Reject before reading any bytes — unsupported types never
            # reach S3 or the extraction queue.
            if not is_allowed_upload_mime(mime_type):
                raise ValidationError(
                    f"Unsupported MIME type {mime_type!r} for blob {file_name}"
                )

            blob_index = meta.blob_id  # use blob_id as index

            key = storage.build_key(
                org_id=str(org_id),
                project_id=str(project_id),
                episode_id=str(episode_id),
                blob_index=blob_index,
                file_name=file_name,
            )

            if isinstance(upload_file, bytes):
                data = await read_upload_bounded(upload_file, max_bytes, file_name)
                content_hash = hashlib.sha256(data).hexdigest()
                await storage.upload(
                    key=key,
                    data=data,
                    mime_type=mime_type,
                )
                file_size = len(data)
            else:
                # One-shot stream — peak memory is one part, not the file.
                content_hash, file_size = await _stream_upload_capped(
                    upload_file, max_bytes, file_name, storage, key, mime_type
                )

            blob_dicts.append(
                {
                    "id": str(uuid.uuid4()),
                    "storage_backend": "s3",
                    "storage_key": key,
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "file_size": file_size,
                    "content_hash": content_hash,
                    "width": None,  # TODO(me): detect image dimensions in phase 2
                    "height": None,
                    "blob_index": blob_index,
                }
            )

        if not blob_dicts:
            return []

        return await self._blob_repo.batch_create(
            organization_id=org_id,
            project_id=project_id,
            session_id=session_id,
            episode_id=episode_id,
            created_by=created_by,
            blobs=blob_dicts,
        )

    # ── Delete ────────────────────────────────────────────────────────────────

    async def delete_blobs(
        self,
        blob_records: Sequence[EpisodeBlob],
        storage_config: dict[str, Any],
    ) -> None:
        """Delete blobs from S3 storage.

        Fail-loud: an S3 failure raises ``S3StorageError`` so callers can
        retry instead of silently leaking objects (or deleting DB rows
        for bytes that still exist).

        Args:
            blob_records: List of EpisodeBlob ORM instances to delete.
            storage_config: Org storage config dict from
                ``OrgConfigBase.to_blob_storage_config()``.

        Raises:
            S3StorageError: If any S3 delete fails.
        """
        if not blob_records:
            return

        config = BlobStorageConfig.from_org_config(storage_config)
        storage = BlobStorage(config)

        # Bound concurrency: one S3 delete per blob, max 10 in flight —
        # an episode with hundreds of blobs must not fan out unbounded
        # connections. Fail-loud is preserved: the first S3StorageError
        # propagates so callers keep DB rows for retry instead of
        # leaking objects.
        _delete_semaphore = asyncio.Semaphore(10)

        async def _delete_one(blob: EpisodeBlob) -> None:
            async with _delete_semaphore:
                await storage.delete(blob.storage_key)

        try:
            await asyncio.gather(*(_delete_one(b) for b in blob_records))
        except S3StorageError:
            logger.warning(
                "blob_storage_service.delete_failed",
                blob_count=len(blob_records),
            )
            raise
        logger.info(
            "blob_storage_service.delete_success",
            blob_count=len(blob_records),
        )

    # ── Validation ──────────────────────────────────────────────────────────

    @staticmethod
    def validate_blob_metadata_count(
        blob_metadatas: Sequence[BlobMetadata],
    ) -> None:
        """Validate per-message blob count limits.

        Args:
            blob_metadatas: List of ``BlobMetadata`` for one message.

        Raises:
            ValidationError: If the count exceeds the per-message limit.
        """
        settings = get_settings()
        if len(blob_metadatas) > settings.MAX_BLOBS_PER_MESSAGE:
            raise ValidationError(
                f"Too many blobs per message: {len(blob_metadatas)} exceeds "
                f"limit of {settings.MAX_BLOBS_PER_MESSAGE}"
            )

    # ── Presigned URL ───────────────────────────────────────────────────────

    @staticmethod
    async def generate_download_url(
        storage_key: str,
        storage_config: dict[str, Any],
        expires_in: int = 300,
    ) -> str:
        """Generate a presigned download URL for a blob (cached).

        Results are cached in-memory keyed by
        ``(endpoint, bucket, key, expires_in)`` for
        ``min(expires_in, 300)`` seconds — a cached URL is never served
        past its own signature expiry. Context assembly fans this out once
        per blob per request, so the cache turns repeat views of the same
        episode into zero S3 signer calls.

        Args:
            storage_key: S3 object key.
            storage_config: Org storage config dict.
            expires_in: URL TTL in seconds (default 5 minutes).

        Returns:
            The presigned URL string.

        Raises:
            S3StorageError: If URL generation fails — propagated to the
                caller so the failure is loud and alertable (no silent
                ``None`` that callers could mistake for success).
        """
        cache_key = (
            str(storage_config.get("endpoint_url") or ""),
            str(storage_config.get("bucket_name") or ""),
            storage_key,
            expires_in,
        )
        now = time.monotonic()
        if (hit := _presigned_url_cache.get(cache_key)) is not None:
            url, cached_at = hit
            if now - cached_at < min(expires_in, _PRESIGNED_URL_TTL_SEC):
                return url
        if len(_presigned_url_cache) >= _PRESIGNED_URL_CACHE_MAX:
            # shortcut: purge expired entries only (amortized — runs on
            # overflow, not per call); a full cache of live entries simply
            # skips caching until entries age out.
            for k, (_, ts) in list(_presigned_url_cache.items()):
                if now - ts >= min(k[3], _PRESIGNED_URL_TTL_SEC):
                    del _presigned_url_cache[k]
        config = BlobStorageConfig.from_org_config(storage_config)
        storage = BlobStorage(config)
        url = await storage.get_presigned_url(storage_key, expires_in=expires_in)
        _presigned_url_cache[cache_key] = (url, now)
        return url
