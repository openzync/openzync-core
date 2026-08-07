"""Memory service — business logic for message ingestion and memory management.

This is the primary entry point for persisting agent memory. The service:

1. Resolves or creates users and sessions
2. Validates and persists messages as episodes in PostgreSQL
3. Enqueues ARQ worker tasks for async enrichment (enrich_episode,
   embed_episode, link_entities_to_episode)
4. Manages idempotency (Redis) and content-level deduplication via an
   atomic claim on the ``ingest_dedup`` table, with Redis as a fast-path
   pre-check only
5. Supports full memory wipe (soft-delete all episodes + facts)

Separation: service orchestrates, repositories query. No SQLAlchemy
expressions in this file.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis
    from sqlalchemy.ext.asyncio import AsyncSession

    from models.session import Session
    from models.user import User

# Import for type hints only; blob uploads are processed before passing to
# the worker, and UploadFile isn't available in the worker context.
from fastapi import UploadFile  # noqa: TCH002 — used in method signature
from sqlalchemy.ext.asyncio import AsyncSession

from core.arq import get_arq
from core.config import get_settings
from core.events import EventType
from core.exceptions import ConflictError, NotFoundError
from repositories.episode_blob_repository import EpisodeBlobRepository
from repositories.episode_repository import EpisodeRepository
from repositories.fact_repository import FactRepository
from repositories.ingest_dedup_repository import IngestDedupRepository
from repositories.organization_repository import OrganizationRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from schemas.memory import IngestMemoryResponse, Message
from services.idempotency_service import IdempotencyService, IdempotencyStatus
from services.webhook_service import WebhookService
from services.worker.worker_settings import get_queue_name

logger = logging.getLogger(__name__)

# ╠ This file contains NO SQLAlchemy expressions.
# ╠ If you see a ``select()`` or ``where()``, it belongs in the repository.

# ── Constants ────────────────────────────────────────────────────────────────

CONTEXT_CACHE_PATTERN = "ctx:{org_id}:{project_id}:*"
"""Redis key pattern for context cache entries to invalidate."""

ARQ_TASKS = [
    # Replaces classify_dialog, extract_entities, extract_facts,
    # and extract_structured.
    "enrich_episode",
    "link_entities_to_episode",
    "embed_episode",
]
"""ARQ worker task names enqueued after a successful ingestion."""

ARQ_QUEUE = "high"
"""ARQ queue name for ingestion-related background tasks."""


class MemoryService:
    """Service layer for message ingestion and memory management.

    ``org_id`` is passed as a parameter to ``ingest()`` and
    ``delete_user_memory()``, not stored on the instance — every public
    method explicitly accepts tenant context for auditability.

    Args:
        db: An async SQLAlchemy session (request-scoped).
        redis_client: An async Redis client for caching and idempotency.
        episode_repo: Repository for episode CRUD.
        session_repo: Repository for session CRUD.
        user_repo: Repository for user CRUD.
        fact_repo: Repository for fact CRUD (used in memory wipe).
    """

    def __init__(
        self,
        db: AsyncSession,
        redis_client: AsyncRedis,
        episode_repo: EpisodeRepository | None = None,
        session_repo: SessionRepository | None = None,
        user_repo: UserRepository | None = None,
        fact_repo: FactRepository | None = None,
        webhook_service: WebhookService | None = None,
        org_repo: OrganizationRepository | None = None,
        blob_repo: EpisodeBlobRepository | None = None,
        idempotency_service: IdempotencyService | None = None,
        dedup_repo: IngestDedupRepository | None = None,
    ) -> None:
        self._db = db
        self._redis = redis_client
        self._webhook_service = webhook_service
        self._idem = idempotency_service or IdempotencyService(redis_client)

        # Repositories (injected or auto-created)
        self._episode_repo = episode_repo or EpisodeRepository(db)
        self._session_repo = session_repo or SessionRepository(db)
        self._user_repo = user_repo or UserRepository(db)
        self._fact_repo = fact_repo or FactRepository(db)
        self._org_repo = org_repo or OrganizationRepository(db)
        self._blob_repo = blob_repo or EpisodeBlobRepository(db)
        self._dedup_repo = dedup_repo or IngestDedupRepository(db)

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    async def ingest(
        self,
        org_id: UUID,
        project_id: UUID,
        created_by: UUID,
        session_external_id: str | None,
        messages: list[Message],
        uploaded_blobs: list[UploadFile] | None = None,
        idempotency_key: str | None = None,
        body_hash: str | None = None,
    ) -> IngestMemoryResponse:
        """Ingest messages into a project's memory.

        Flow:
        1. Idempotency check (Redis) — return cached response if duplicate,
           raise ``ConflictError`` if the key was used with a different body.
        2. Resolve or create the session (``__default__`` if omitted).
        3. Compute content hash for content-level dedup (via IdempotencyService).
        4. Redis fast-path pre-check for dedup (fast-path ONLY — the
           authoritative arbiter is the ingest_dedup claim in step 5).
        5. Atomically claim the batch in ``ingest_dedup`` (DB-level dedup,
           TOCTOU-safe — the claim shares the caller's transaction).
        6. Get next sequence number for ordered insertion.
        7. Build episode dicts from validated messages.
        8. PII detection & redaction (if enabled in org quotas).
        9. Batch-insert episodes into PostgreSQL.
        10. Upload blobs to S3 and persist blob records.
        11. Enqueue ARQ enrichment tasks (enrich_episode, embed_episode,
            link_entities_to_episode) + blob text extraction tasks.
        12. Store idempotency key and content hash (payload = job_id)
            for future dedup.
        13. Invalidate context cache for this project.
        14. Return 202 ``IngestMemoryResponse``.

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID for scoping.
            created_by: The authenticated user's UUID (attribution).
            session_external_id: Optional session external ID.
                Auto-creates ``__default__`` if omitted.
            messages: List of validated message objects.
            uploaded_blobs: Optional list of uploaded files from a multipart
                request. Indexed by ``BlobMetadata.blob_id`` in each message.
            idempotency_key: Optional ``Idempotency-Key`` header value
                for request-level deduplication.
            body_hash: Optional SHA-256 digest of the canonical request body,
                pre-computed by the router when ``idempotency_key`` is set.
                Used to detect key reuse with a different payload.

        Returns:
            An ``IngestMemoryResponse`` with job_id, episode_count,
            and blob_count.

        Raises:
            ConflictError: If ``idempotency_key`` was already used with a
                different request body.
        """
        # ── Step 1: Idempotency check ────────────────────────────────────
        if idempotency_key is not None:
            result = await self._idem.check_idempotency_key(
                idempotency_key, body_hash or "", str(org_id)
            )
            if (
                result.status == IdempotencyStatus.REPLAY
                and result.response_data is not None
            ):
                logger.info(
                    "memory.idempotency_replay",
                    extra={
                        "idempotency_key": idempotency_key[:16] + "...",
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                    },
                )
                return IngestMemoryResponse(**result.response_data)
            if result.status == IdempotencyStatus.CONFLICT:
                raise ConflictError(
                    "Idempotency-Key already used with a different request body"
                )

        # ── Step 2: Resolve or create session ────────────────────────────
        session = await self._resolve_session(
            organization_id=org_id,
            project_id=project_id,
            created_by=created_by,
            session_external_id=session_external_id,
        )
        session_id = session.id
        logger.debug(
            "memory.session_resolved",
            extra={
                "session_id": str(session_id),
                "external_id": session.external_id,
                "project_id": str(project_id),
                "created_by": str(created_by),
            },
        )

        # ── Step 3: Content-level dedup ──────────────────────────────────
        msgs = [m.model_dump() for m in messages]
        content_hash = self._idem.compute_content_hash(
            str(org_id), str(created_by), str(session_id), msgs
        )
        # Redis fast-path pre-check ONLY — never relied on for correctness.
        # The authoritative dedup arbiter is the ingest_dedup claim below,
        # which serializes concurrent identical submissions in the DB.
        existing_job_id = await self._idem.check_content_hash(
            str(org_id), str(created_by), str(session_id), msgs
        )
        if existing_job_id is not None:
            logger.info(
                "memory.content_dedup_hit",
                extra={
                    "content_hash": content_hash[:16] + "...",
                    "existing_job_id": existing_job_id,
                    "project_id": str(project_id),
                },
            )
            return IngestMemoryResponse(
                job_id=existing_job_id,
                episode_count=len(messages),
                status="accepted",
                message="Content already ingested; returning existing job_id",
            )

        # ── Step 4: Claim the batch (TOCTOU-safe dedup) ──────────────────
        # job_id is generated before the claim so the accepted ingest can be
        # referenced by both the dedup row and the ARQ enrichment tasks.
        # The claim shares the caller's transaction: it commits atomically
        # with the episodes below, and a concurrent identical submission
        # that loses the claim returns a duplicate response instead.
        job_id = uuid4()
        if not await self._dedup_repo.insert_or_none(
            project_id=project_id,
            session_id=session_id,
            content_hash=content_hash,
            job_id=job_id,
        ):
            prior_job_id = await self._dedup_repo.get_job_id(
                project_id=project_id,
                session_id=session_id,
                content_hash=content_hash,
            )
            logger.info(
                "memory.content_dedup_hit",
                extra={
                    "content_hash": content_hash,
                    "existing_job_id": str(prior_job_id) if prior_job_id else None,
                    "project_id": str(project_id),
                },
            )
            return IngestMemoryResponse(
                job_id=str(prior_job_id) if prior_job_id else None,
                episode_count=len(messages),
                status="accepted",
                message="Content already ingested; returning existing job_id",
            )

        # ── Step 5: Get next sequence number ──────────────────────────────
        start_seq = await self._episode_repo.get_next_sequence(session_id)

        # ── Step 6: Build episode dicts ───────────────────────────────────
        episode_dicts = [
            {
                "role": msg.role,
                "content": msg.content,
                "metadata": msg.metadata,
                "created_at": msg.created_at,
                "sequence_number": start_seq + i,
            }
            for i, msg in enumerate(messages)
        ]

        # ── Step 7: PII detection & redaction ─────────────────────────────
        pii_config_raw = await self._get_org_pii_config(org_id)
        pii_mode = (
            pii_config_raw.get("mode", "off")
            if isinstance(pii_config_raw, dict)
            else "off"
        )

        if pii_mode != "off":
            from services.pii_service import PIIService

            pii_service = PIIService(pii_config_raw)
            for msg_dict in episode_dicts:
                content = msg_dict["content"]
                redacted, detections, was_blocked = await pii_service.process_message(
                    content
                )
                if redacted != content:
                    msg_dict["content"] = redacted

        # ── Step 8: Batch-insert episodes ────────────────────────────────
        episodes = await self._episode_repo.batch_create(
            organization_id=org_id,
            session_id=session_id,
            project_id=project_id,
            user_id=created_by,
            messages=episode_dicts,
        )
        logger.info(
            "memory.episodes_created",
            extra={
                "count": len(episodes),
                "session_id": str(session_id),
                "project_id": str(project_id),
                "org_id": str(org_id),
            },
        )

        # ── Step 9: Upload blobs and persist blob records ────────────────
        blob_count = 0
        blob_records: list[Any] = []
        if uploaded_blobs:
            blob_count, blob_records = await self._process_blobs(
                org_id=org_id,
                project_id=project_id,
                session_id=session_id,
                created_by=created_by,
                episodes=episodes,
                messages=messages,
                uploaded_blobs=uploaded_blobs,
            )

        # ── Commit so workers can see episodes + blobs before tasks ─────
        await self._db.commit()

        # ── Step 10: Enqueue ARQ tasks with the claimed job_id ───────────
        episode_dicts = [
            {
                "id": ep.id,
                "content": ep.content,
                "role": ep.role,
                "metadata": ep.metadata_,
            }
            for ep in episodes
        ]
        await self._enqueue_arq_tasks(
            job_id=str(job_id),
            org_id=str(org_id),
            project_id=str(project_id),
            session_id=str(session_id),
            episodes=episode_dicts,
        )

        # ── Step 10b: Enqueue blob text extraction tasks ─────────────────
        if blob_records:
            await self._enqueue_blob_extraction_tasks(
                blob_records=blob_records,
                org_id=org_id,
                project_id=project_id,
            )

        # ── Step 9: Store idempotency key and content hash ──────────────
        response = IngestMemoryResponse(
            job_id=str(job_id),
            episode_count=len(episodes),
            blob_count=blob_count,
            status="accepted",
            message="Messages accepted for processing",
        )

        if idempotency_key is not None:
            await self._idem.store_idempotency_key(
                idempotency_key, body_hash or "", response.model_dump(), str(org_id)
            )

        await self._idem.store_content_hash(
            str(org_id), str(created_by), str(session_id), msgs, payload=str(job_id)
        )

        # ── Step 12: Invalidate context cache for this project ───────────
        await self._invalidate_context_cache(str(org_id), str(project_id))

        # ── Step 13: Emit webhook events ─────────────────────────────────
        if self._webhook_service:
            event_payload = {
                "org_id": str(org_id),
                "project_id": str(project_id),
                "session_id": str(session_id),
                "episode_count": len(episodes),
                "job_id": str(job_id),
            }
            await self._webhook_service.emit(
                organization_id=org_id,
                event_type=EventType.INGEST_BATCH_COMPLETED,
                payload=event_payload,
            )
            await self._webhook_service.emit(
                organization_id=org_id,
                event_type=EventType.MESSAGE_ADDED,
                payload=event_payload,
            )

        return response

    async def delete_project_memory(
        self,
        org_id: UUID,
        project_id: UUID,
    ) -> tuple[int, int]:
        """Soft-delete all memory (episodes + facts) for a project.

        This is the GDPR / memory-wipe operation for a project. It does
        **not** delete sessions — only the data within them.

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID.

        Returns:
            Tuple of ``(episodes_deleted, facts_deleted)`` counts.
        """
        episodes_deleted = await self._episode_repo.soft_delete_by_project(project_id)
        facts_deleted = await self._fact_repo.soft_delete_by_project(project_id)

        logger.info(
            "memory.project_memory_deleted",
            extra={
                "project_id": str(project_id),
                "org_id": str(org_id),
                "episodes_deleted": episodes_deleted,
                "facts_deleted": facts_deleted,
            },
        )

        return episodes_deleted, facts_deleted

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _resolve_user(
        self,
        org_id: UUID,
        external_id: str,
    ) -> User:
        """Resolve a user by external_id, creating one if it does not exist.

        Thread-safe via the ``(organization_id, external_id)`` unique
        constraint — concurrent creates are handled with an IntegrityError
        retry in the repository layer.

        Args:
            org_id: The organization UUID.
            external_id: The caller-defined user identifier.

        Returns:
            A ``User`` ORM instance (existing or newly created).
        """
        return await self._user_repo.create_or_get_by_external_id(
            organization_id=org_id,
            external_id=external_id,
        )

    async def _resolve_session(
        self,
        organization_id: UUID,
        project_id: UUID,
        created_by: UUID,
        session_external_id: str | None,
    ) -> Session:
        """Resolve an existing session or auto-create a default one.

        Rules:
        - If ``session_external_id`` is provided: look up the existing
          session and raise ``NotFoundError`` if it does not exist.
          Sessions are NOT auto-created from arbitrary IDs — the SDK
          must call ``POST /sessions`` first.
        - If ``session_external_id`` is ``None``: get or create a session
          named ``__default__``. Uses ``INSERT ... ON CONFLICT DO NOTHING``
          for race safety.

        Args:
            organization_id: The organization UUID.
            project_id: The project UUID.
            created_by: The authenticated user's UUID (attribution).
            session_external_id: The caller-defined session identifier,
                or ``None`` to use the default session.

        Returns:
            A ``Session`` ORM instance.

        Raises:
            NotFoundError: If a specific session_id was given but not found.
        """
        if session_external_id is not None:
            # Try by external_id first (the canonical lookup).
            session = await self._session_repo.get_by_external_id(
                org_id=organization_id,
                project_id=project_id,
                external_id=session_external_id,
            )
            if session is None:
                # Fallback: try resolving as a raw UUID — the caller may
                # have passed the session's internal UUID rather than its
                # user-facing external_id.
                try:
                    parsed = UUID(session_external_id)
                except ValueError:
                    parsed = None
                if parsed is not None:
                    session = await self._session_repo.get_by_uuid(
                        org_id=organization_id,
                        session_id=parsed,
                        project_id=project_id,
                    )
            if session is None:
                raise NotFoundError(
                    f"Session '{session_external_id}' not found in project {project_id}"
                )
            return session

        # Auto-create or get existing "__default__" session
        return await self._session_repo.get_or_create_default(
            org_id=organization_id,
            project_id=project_id,
            created_by=created_by,
        )

    # ── Idempotency & content dedup ──────────────────────────────────────────
    # Delegated to IdempotencyService (self._idem) — see idempotency_service.py.

    # ── PII Config ────────────────────────────────────────────────────────────

    # ── PII Config ────────────────────────────────────────────────────────────

    async def _get_org_pii_config(self, org_id: UUID) -> dict:
        """Fetch PII configuration for an org from their quotas JSONB.

        The PII config lives at ``organizations.quotas -> 'pii'``.  We use a
        raw ``text()`` query instead of a full repository to avoid scope creep —
        this is the only org-level query that ``MemoryService`` needs.

        Args:
            org_id: The organization UUID.

        Returns:
            The PII config dict (possibly empty).  Returns ``{}`` if the
            organization does not exist or has no PII config.
        """
        return await self._org_repo.get_pii_config(org_id)

    # ── ARQ Task Enqueue ─────────────────────────────────────────────────────

    async def _enqueue_arq_tasks(
        self,
        job_id: str,
        org_id: str,
        project_id: str,
        session_id: str,
        episodes: list[dict[str, Any]],
    ) -> None:
        """Enqueue ARQ background tasks for episode enrichment.

        One job per task per episode is enqueued:
        - ``enrich_episode`` (high queue): combined LLM enrichment —
          replaces the legacy ``extract_entities`` / ``extract_facts`` /
          ``classify_dialog`` workers.
        - ``embed_episode`` (high queue): generates embeddings via the
          configured API.
        - ``link_entities_to_episode`` (low queue): links extracted entities
          to the episode.

        If the ARQ pool is unavailable (Redis down), episodes are safe in
        PostgreSQL and will be picked up by a reconciliation worker.

        Args:
            job_id: The composite job ID for this ingestion.
            org_id: The organization UUID string.
            project_id: The project UUID string.
            session_id: The session UUID string.
            episodes: List of episode dicts with ``id``, ``content``, ``role``.
        """
        episode_ids = [ep["id"] for ep in episodes]
        trace_id = structlog.contextvars.get_contextvars().get(
            "request_id", str(uuid4())
        )
        try:
            arq_pool = get_arq()
            env = get_settings().ENVIRONMENT
            qname = get_queue_name(env, "high")
            for episode in episodes:
                ep_id = str(episode["id"])
                content = episode["content"]
                role = episode.get("role", "user")
                metadata = episode.get("metadata", {})
                common = {
                    "episode_id": ep_id,
                    "content": content,
                    "org_id": org_id,
                    "project_id": project_id,
                    "trace_id": trace_id,
                    "metadata": metadata,
                }

                # Single combined enrichment — replaces 4 LLM workers
                await arq_pool.enqueue(
                    "enrich_episode",
                    queue_name=qname,
                    **common,
                    session_id=session_id,
                    role=role,
                )
                await arq_pool.enqueue("embed_episode", queue_name=qname, **common)
                await arq_pool.enqueue(
                    "link_entities_to_episode",
                    queue_name=get_queue_name(env, "low"),
                    **common,
                    role=role,
                )

            logger.info(
                "memory.arq_tasks_enqueued",
                extra={
                    "job_id": job_id,
                    "task_count": len(ARQ_TASKS),
                    "org_id": org_id,
                    "project_id": project_id,
                },
            )
        except Exception:
            logger.critical(
                "memory.arq_enqueue_failed",
                extra={
                    "job_id": job_id,
                    "org_id": org_id,
                    "project_id": project_id,
                    "session_id": session_id,
                    "episode_ids": episode_ids,
                    "error": "ARQ pool unavailable — tasks not enqueued. "
                    "Episodes are safe in PostgreSQL; reconciliation needed.",
                },
            )
            raise  # Propagate so ARQ retry mechanism handles it

    # ── Blob Processing ───────────────────────────────────────────────────────

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

        # Resolve per-org storage config from OpenBao (matches the
        # pattern in extract_blob_text.py and enrich_episode.py).
        from core.config import BootstrapSettings
        from core.openbao import OpenBaoClient
        from core.org_config import get_org_config

        storage_config: dict[str, Any] = {
            "backend": "s3",
            "endpoint_url": "http://minio:9000",
            "region": "auto",
            "access_key_id": "",
            "secret_access_key": "",
            "bucket_name": "openzync-blobs",
            "max_blob_size_mb": 50,
        }
        try:
            bootstrap = BootstrapSettings()
            async with OpenBaoClient(
                bootstrap.OPENBAO_ADDR,
                bootstrap.OPENBAO_ROLE_ID,
                bootstrap.OPENBAO_SECRET_ID,
                timeout=10.0,
            ) as _tmp_bao:
                org_cfg = await get_org_config(
                    org_id, redis=None, bao_client=_tmp_bao,
                )
                org_storage = org_cfg.to_blob_storage_config()
                if org_storage:
                    storage_config.update(org_storage)
        except Exception:
            logger.warning(
                "memory.org_storage_config_fetch_failed",
                extra={"org_id": str(org_id)},
                exc_info=True,
            )
            # Falls back to defaults — works for MinIO in dev

        from services.blob_storage_service import BlobStorageService

        blob_svc = BlobStorageService(self._db, self._blob_repo)
        blob_records: list[Any] = []

        for episode_id, metas in ep_blob_metas:
            records = await blob_svc.upload_blobs(
                org_id=org_id,
                project_id=project_id,
                episode_id=episode_id,
                session_id=session_id,
                created_by=created_by,
                uploaded_files=uploaded_blobs,
                blob_metadatas=metas,
                storage_config=storage_config,
            )
            blob_records.extend(records)

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

    # ── Blob Extraction Task Enqueue ─────────────────────────────────────────

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
        trace_id = structlog.contextvars.get_contextvars().get(
            "request_id", str(uuid4())
        )
        try:
            arq_pool = get_arq()
            low_qname = get_queue_name(get_settings().ENVIRONMENT, "low")
            for blob in blob_records:
                await arq_pool.enqueue(
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

    # ── Context Cache Invalidation ───────────────────────────────────────────

    async def _invalidate_context_cache(self, org_id: str, project_id: str) -> None:
        """Invalidate all context cache entries for a project.

        Called after ingestion so that subsequent context-assembly
        queries fetch fresh data from the database.

        Uses Redis ``SCAN`` + ``DEL`` to match the pattern
        ``ctx:{org_id}:{project_id}:*``.

        Args:
            org_id: The organization UUID string.
            project_id: The project UUID string.
        """
        pattern = CONTEXT_CACHE_PATTERN.format(org_id=org_id, project_id=project_id)
        cursor: int = 0
        deleted = 0
        while True:
            cursor, keys = await self._redis.scan(
                cursor=cursor, match=pattern, count=100
            )
            if keys:
                deleted += await self._redis.delete(*keys)
            if cursor == 0:
                break
        if deleted > 0:
            logger.debug(
                "memory.context_cache_invalidated",
                extra={
                    "org_id": org_id,
                    "project_id": project_id,
                    "keys_deleted": deleted,
                },
            )
