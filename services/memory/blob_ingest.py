"""Blob ingest helpers — S3 upload, compensation, extraction enqueue."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import structlog

from core.exceptions import ValidationError

if TYPE_CHECKING:
    from fastapi import UploadFile

    from schemas.memory import Message

logger = logging.getLogger(__name__)


class BlobIngestMixin:
    """S3-backed blob upload with pre-commit compensation."""

    _db: Any
    _redis: Any
    _bao_client: Any
    _blob_repo: Any

    async def _resolve_storage_config(self, org_id: UUID) -> dict[str, Any]:
        """Resolve the org's blob storage config from OpenBao — fail loud.

        The org's S3 settings are read with the app-level OpenBao client
        (an AppRole login per call is both slow and needless) and the
        app-level Redis client, so the 300 s org-config cache is used.

        A storage misconfiguration must never degrade to the shared dev
        bucket: an org that configured its own bucket would silently have
        its documents written to ``openzync-blobs`` instead.  Any failure
        to read the org's config therefore raises
        :class:`ExternalServiceError` (→ 502) rather than falling back to
        the defaults.

        Args:
            org_id: The organization UUID.

        Returns:
            Flattened storage config dict for ``BlobStorageConfig``.

        Raises:
            ExternalServiceError: If the org's storage config cannot be read.
        """
        from core.exceptions import ExternalServiceError
        from core.org_config import get_org_config

        try:
            org_cfg = await get_org_config(
                org_id,
                redis=self._redis,
                bao_client=self._bao_client,
            )
        except Exception as exc:
            logger.error(
                "memory.org_storage_config_fetch_failed",
                extra={"org_id": str(org_id)},
                exc_info=True,
            )
            raise ExternalServiceError(
                f"Blob storage config for org {org_id} could not be read from OpenBao."
            ) from exc

        # Platform defaults (the shared MinIO bucket of the compose stack)
        # are overridden by whatever the org has configured explicitly.
        storage_config: dict[str, Any] = {
            "backend": "s3",
            "endpoint_url": "http://minio:9000",
            "region": "auto",
            "access_key_id": "",
            "secret_access_key": "",
            "bucket_name": "openzync-blobs",
            "max_blob_size_mb": 50,
        }
        storage_config.update(org_cfg.to_blob_storage_config())
        return storage_config

    async def _compensate_blob_uploads(
        self, org_id: UUID, storage_keys: list[str]
    ) -> None:
        """Delete S3 objects orphaned by a pre-commit ingest failure.

        Uploads landed in S3 but the transaction rolled back, so no blob
        row references them and the orphan-blob GC worker cannot see them.
        Loud by design: every failure is logged with the orphaned key, and
        this method never raises — it must not mask the original exception.

        Args:
            org_id: The organization UUID (storage config scope).
            storage_keys: S3 keys uploaded by the failed attempt.
        """
        if not storage_keys:
            return
        try:
            from core.blob_storage import BlobStorage, BlobStorageConfig

            storage_config = await self._resolve_storage_config(org_id)
            storage = BlobStorage(
                BlobStorageConfig.from_org_config(storage_config)
            )
            for key in storage_keys:
                try:
                    await storage.delete(key)
                except Exception:
                    logger.error(
                        "memory.blob_compensating_delete_failed",
                        extra={"org_id": str(org_id), "storage_key": key},
                        exc_info=True,
                    )
        except Exception:
            logger.error(
                "memory.blob_compensation_failed",
                extra={"org_id": str(org_id), "key_count": len(storage_keys)},
                exc_info=True,
            )

    async def _process_blobs(
        self,
        org_id: UUID,
        project_id: UUID,
        session_id: UUID,
        created_by: UUID,
        episodes: list[Any],  # Episode ORM models
        messages: list[Message],
        uploaded_blobs: list[UploadFile],
    ) -> tuple[int, list[Any]]:
        """Upload blobs to S3 and persist their metadata in the DB.

        Iterates messages alongside their corresponding episode IDs,
        collects blob metadata per episode, and delegates to
        ``BlobStorageService.upload_blobs`` for each batch.

        Args:
            org_id: Organization UUID.
            project_id: Project UUID.
            session_id: Session UUID.
            created_by: User UUID who uploaded the blobs.
            episodes: List of ``Episode`` ORM models returned by
                ``batch_create``, ordered by message index.
            messages: The original validated message objects (same order
                as ``episodes``).
            uploaded_blobs: The ``UploadFile`` objects from the multipart
                request.

        Returns:
            Tuple of ``(blob_count, blob_records)`` where ``blob_records``
            is the list of ``EpisodeBlob`` ORM instances created.
        """
        # Build per-episode blob metadata from messages that have blobs.
        # Pass BlobMetadata instances directly (typed schema, not raw dicts).
        ep_blob_metas: list[tuple[UUID, list[Any]]] = []
        for msg_idx, msg in enumerate(messages):
            if not msg.blobs:
                continue
            episode_id = episodes[msg_idx].id
            ep_blob_metas.append((episode_id, list(msg.blobs)))

        if not ep_blob_metas:
            return 0, []

        # Resolved once per ingest — not per blob.
        storage_config = await self._resolve_storage_config(org_id)

        from core.blob_storage import BlobStorage, BlobStorageConfig
        from services.blob_storage_service import (
            BlobStorageService,
            read_upload_bounded,
        )

        config = BlobStorageConfig.from_org_config(storage_config)
        max_bytes = config.max_blob_bytes
        # One S3 client for the whole ingest — not one per episode.
        storage = BlobStorage(config)
        # Stream per episode — read only the files each episode references
        # so peak RSS is one episode's blobs plus still-unconsumed shared
        # files, not the whole request. UploadFile.read() is a one-shot
        # stream, so files referenced by multiple episodes stay cached until
        # their last referencing episode uploads, then are released.
        # Every cached blob is capped at max_blob_bytes by
        # read_upload_bounded — shared files cannot blow the cap silently.
        use_counts = Counter(
            meta.blob_id for _, metas in ep_blob_metas for meta in metas
        )
        shared_ids = {bid for bid, n in use_counts.items() if n > 1}
        shared_bytes: dict[int, bytes] = {}
        remaining = dict(use_counts)

        blob_svc = BlobStorageService(self._db, self._blob_repo)
        blob_records: list[Any] = []

        for episode_id, metas in ep_blob_metas:
            needed_ids = {meta.blob_id for meta in metas}
            slots: list[UploadFile | bytes] = [b""] * len(uploaded_blobs)
            for bid in needed_ids:
                if bid >= len(uploaded_blobs):
                    raise ValidationError(
                        f"blob_id {bid} out of range: "
                        f"only {len(uploaded_blobs)} files uploaded"
                    )
                cached = shared_bytes.get(bid)
                if cached is not None:
                    slots[bid] = cached
                    continue
                data = await read_upload_bounded(
                    uploaded_blobs[bid],
                    max_bytes,
                    uploaded_blobs[bid].filename or "unnamed",
                )
                slots[bid] = data
                if bid in shared_ids:
                    shared_bytes[bid] = data
            records = await blob_svc.upload_blobs(
                org_id=org_id,
                project_id=project_id,
                episode_id=episode_id,
                session_id=session_id,
                created_by=created_by,
                uploaded_files=slots,
                blob_metadatas=metas,
                storage_config=storage_config,
                storage=storage,
            )
            blob_records.extend(records)
            # Release shared files whose last referencing episode just
            # uploaded — without this the cache holds every shared file
            # until return (peak = whole request for fully-shared blobs).
            for bid in needed_ids:
                if bid in shared_ids:
                    remaining[bid] -= 1
                    if remaining[bid] <= 0:
                        del shared_bytes[bid]

        # blob_count reflects persisted blob records (one per referenced
        # file that actually uploaded) — not metadata entries. A blobs
        # array with no uploaded files is rejected in ingest() before this
        # runs, so 0 here genuinely means "no blobs", never "files lost".
        blob_count = len(blob_records)
        logger.info(
            "memory.blobs_uploaded",
            extra={
                "blob_count": blob_count,
                "org_id": str(org_id),
                "project_id": str(project_id),
            },
        )
        return blob_count, blob_records

    async def _enqueue_blob_extraction_tasks(
        self,
        blob_records: list[Any],
        org_id: UUID,
        project_id: UUID,
    ) -> None:
        """Enqueue ARQ ``extract_blob_text`` tasks for each uploaded blob.

        Runs AFTER the DB commit so workers can query the blob records.
        Blob text extraction is non-critical — if ARQ is unavailable the
        blobs are already safe in S3 and DB, and a reconciliation worker
        can catch up later.

        Args:
            blob_records: List of ``EpisodeBlob`` ORM instances returned
                by ``_process_blobs``.
            org_id: Organization UUID.
            project_id: Project UUID.
        """
        from core.config import get_settings
        from core.queue_names import get_queue_name
        from services import memory_service as _facade  # via facade (patchable)

        trace_id = structlog.contextvars.get_contextvars().get(
            "request_id", str(uuid4())
        )
        try:
            arq_pool = _facade.get_arq()
            low_qname = get_queue_name(get_settings().ENVIRONMENT, "low")
            await asyncio.gather(
                *(
                    arq_pool.enqueue(
                        "extract_blob_text",
                        queue_name=low_qname,
                        blob_id=str(blob.id),
                        org_id=str(org_id),
                        project_id=str(project_id),
                        episode_id=str(blob.episode_id),
                        storage_key=blob.storage_key,
                        mime_type=blob.mime_type,
                        file_name=blob.file_name,
                        trace_id=trace_id,
                    )
                    for blob in blob_records
                )
            )
            logger.info(
                "memory.blob_extraction_tasks_enqueued",
                extra={
                    "blob_count": len(blob_records),
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
        except Exception:
            logger.critical(
                "memory.blob_extraction_enqueue_failed",
                extra={
                    "blob_count": len(blob_records),
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "error": (
                        "ARQ pool unavailable — blob text extraction not enqueued. "
                        "Blobs are safe in S3 and DB; reconciliation needed."
                    ),
                },
            )
