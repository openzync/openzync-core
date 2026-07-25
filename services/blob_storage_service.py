"""Blob storage orchestration — upload, download, delete, text extraction.

Sits between ``MemoryService`` (business logic) and ``BlobStorage`` (S3 client).
Handles org config resolution, blob-key construction, file validation,
and DB record creation.
"""

from __future__ import annotations

import hashlib
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
        uploaded_files: Sequence[UploadFile],
        blob_metadatas: Sequence["BlobMetadata"],
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
                request.
            blob_metadatas: List of dicts with ``blob_id``, ``file_name``,
                ``mime_type`` keys from the request's ``BlobMetadata``
                schemas.  The ``blob_id`` indexes into ``uploaded_files``.
            storage_config: Flattened org storage config from
                ``OrgConfigBase.to_blob_storage_config()``.

        Returns:
            List of created ``EpisodeBlob`` ORM instances.

        Raises:
            ValidationError: If blob metadata references are out of range,
                or the total blob count exceeds ``MAX_BLOBS_PER_REQUEST``.
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
            data = await upload_file.read()

            if len(data) > max_bytes:
                raise PayloadTooLargeError(
                    f"Blob {meta.file_name} "
                    f"({len(data)} bytes) exceeds limit of {max_bytes} bytes"
                )

            content_hash = hashlib.sha256(data).hexdigest()
            blob_index = meta.blob_id  # use blob_id as index
            file_name = meta.file_name or upload_file.filename or "unnamed"
            mime_type = meta.mime_type or upload_file.content_type or "application/octet-stream"

            key = storage.build_key(
                org_id=str(org_id),
                project_id=str(project_id),
                episode_id=str(episode_id),
                blob_index=blob_index,
                file_name=file_name,
            )

            await storage.upload(
                key=key,
                data=data,
                mime_type=mime_type,
            )

            blob_dicts.append({
                "id": str(uuid.uuid4()),
                "storage_backend": "s3",
                "storage_key": key,
                "file_name": file_name,
                "mime_type": mime_type,
                "file_size": len(data),
                "content_hash": content_hash,
                "width": None,  # TODO(me): detect image dimensions in phase 2
                "height": None,
                "blob_index": blob_index,
            })

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

        Best-effort: logs failures but does not raise. Callers should
        commit the DB deletion regardless of S3 result — lifecycle
        policies provide the safety net for orphaned objects.

        Args:
            blob_records: List of EpisodeBlob ORM instances to delete.
            storage_config: Org storage config dict from
                ``OrgConfigBase.to_blob_storage_config()``.
        """
        if not blob_records:
            return

        config = BlobStorageConfig.from_org_config(storage_config)
        storage = BlobStorage(config)

        for blob in blob_records:
            try:
                await storage.delete(blob.storage_key)
                logger.info(
                    "blob_storage_service.delete_success",
                    blob_id=str(blob.id),
                    key=blob.storage_key,
                )
            except S3StorageError:
                logger.warning(
                    "blob_storage_service.delete_failed",
                    blob_id=str(blob.id),
                    key=blob.storage_key,
                )

    # ── Validation ──────────────────────────────────────────────────────────

    @staticmethod
    def validate_blob_metadata_count(
        blob_metadatas: Sequence["BlobMetadata"],
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
    ) -> str | None:
        """Generate a presigned download URL for a blob.

        Returns ``None`` if URL generation fails (caller should serve a
        temporary-unavailable placeholder).

        Args:
            storage_key: S3 object key.
            storage_config: Org storage config dict.
            expires_in: URL TTL in seconds (default 5 minutes).

        Returns:
            Presigned URL string, or ``None`` on failure.
        """
        try:
            config = BlobStorageConfig.from_org_config(storage_config)
            storage = BlobStorage(config)
            return await storage.get_presigned_url(storage_key, expires_in=expires_in)
        except S3StorageError:
            logger.warning(
                "blob_storage_service.download_url_failed",
                key=storage_key,
            )
            return None
