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
    _blob_repo: Any

    async def _compensate_blob_uploads(
        self, user_id: UUID, storage_keys: list[str]
    ) -> None:
        """Delete S3 objects orphaned by a pre-commit ingest failure.

        Uploads landed in S3 but the transaction rolled back, so no blob
        row references them and the orphan-blob GC worker cannot see them.
        Loud by design: every failure is logged with the orphaned key, and
        this method never raises — it must not mask the original exception.

        Args:
            user_id: The organization UUID (storage config scope).
            storage_keys: S3 keys uploaded by the failed attempt.
        """
        if not storage_keys:
            return
        try:
            from core.blob_storage import BlobStorage, BlobStorageConfig
            from core.config import get_settings

            # Global backend — fail-fast Settings, never per-org.
            storage = BlobStorage(BlobStorageConfig.from_settings(get_settings()))
            for key in storage_keys:
                try:
                    await storage.delete(key)
                except Exception:
                    logger.error(
                        "memory.blob_compensating_delete_failed",
                        extra={"user_id": str(user_id), "storage_key": key},
                        exc_info=True,
                    )
        except Exception:
            logger.error(
                "memory.blob_compensation_failed",
                extra={"user_id": str(user_id), "key_count": len(storage_keys)},
                exc_info=True,
            )

    async def _process_blobs(
        self,
        user_id: UUID,
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
            user_id: Organization UUID.
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

        # Global backend built once per ingest — not one client per episode.
        from core.blob_storage import BlobStorage, BlobStorageConfig
        from core.config import get_settings
        from services.blob_storage_service import (
            BlobStorageService,
            read_upload_bounded,
        )

        storage = BlobStorage(BlobStorageConfig.from_settings(get_settings()))
        max_bytes = storage.max_blob_bytes
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
                user_id=user_id,
                project_id=project_id,
                episode_id=episode_id,
                session_id=session_id,
                created_by=created_by,
                uploaded_files=slots,
                blob_metadatas=metas,
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
                "user_id": str(user_id),
                "project_id": str(project_id),
            },
        )
        return blob_count, blob_records

    async def _enqueue_blob_extraction_tasks(
        self,
        blob_records: list[Any],
        user_id: UUID,
        project_id: UUID,
        enrichments: dict[str, bool] | None = None,
    ) -> None:
        """Enqueue ARQ ``extract_blob_text`` tasks for each uploaded blob.

        Runs AFTER the DB commit so workers can query the blob records.
        Blob text extraction is non-critical — if ARQ is unavailable the
        blobs are already safe in S3 and DB, and a reconciliation worker
        can catch up later.

        Args:
            blob_records: List of ``EpisodeBlob`` ORM instances returned
                by ``_process_blobs``.
            user_id: Organization UUID.
            project_id: Project UUID.
            enrichments: Effective per-episode toggles (forwarded into
                every ARQ payload; workers mark-skipped disabled bits).
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
                        user_id=str(user_id),
                        project_id=str(project_id),
                        episode_id=str(blob.episode_id),
                        storage_key=blob.storage_key,
                        mime_type=blob.mime_type,
                        file_name=blob.file_name,
                        trace_id=trace_id,
                        enrichments=enrichments,
                    )
                    for blob in blob_records
                )
            )
            logger.info(
                "memory.blob_extraction_tasks_enqueued",
                extra={
                    "blob_count": len(blob_records),
                    "user_id": str(user_id),
                    "project_id": str(project_id),
                },
            )
        except Exception:
            logger.critical(
                "memory.blob_extraction_enqueue_failed",
                extra={
                    "blob_count": len(blob_records),
                    "user_id": str(user_id),
                    "project_id": str(project_id),
                    "error": (
                        "ARQ pool unavailable — blob text extraction not enqueued. "
                        "Blobs are safe in S3 and DB; reconciliation needed."
                    ),
                },
            )
