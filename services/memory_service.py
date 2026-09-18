"""Memory service — business logic for message ingestion and memory management.

This is the primary entry point for persisting agent memory. The service:

1. Resolves or creates users and resolves sessions
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.arq import get_arq
from core.config import get_settings
from core.events import EventType
from core.exceptions import (
    ConflictError,
    NotFoundError,
    PIIUnavailableError,
    ValidationError,
)
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


def _encode_claim_payload(job_id: UUID, episode_count: int) -> str:
    """Encode the dedup claim payload as ``"{job_id}:{episode_count}"``.

    The count travels with the claim so a replay returns the winner's
    ``episode_count`` instead of recomputing it from the loser's request.
    """
    return f"{job_id}:{episode_count}"


def _decode_claim_payload(
    payload: str, *, fallback_count: int
) -> tuple[str, int]:
    """Split a claim payload into ``(job_id, episode_count)``.

    Legacy payloads stored a bare ``job_id`` (no ``:count`` suffix) —
    those fall back to ``fallback_count``.

    Args:
        payload: The stored claim value (winner's payload on replay).
        fallback_count: Count to use when the payload carries none.

    Returns:
        Tuple of ``(job_id, episode_count)``.
    """
    job_id, sep, count_str = payload.partition(":")
    if not sep:
        return payload, fallback_count
    try:
        return job_id, int(count_str)
    except ValueError:
        return job_id, fallback_count


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
        bao_client: Any | None = None,
    ) -> None:
        self._db = db
        self._redis = redis_client
        self._webhook_service = webhook_service
        self._idem = idempotency_service or IdempotencyService(redis_client)
        self._bao_client = bao_client

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
        session_external_id: str,
        messages: list[Message],
        uploaded_blobs: list[UploadFile] | None = None,
        idempotency_key: str | None = None,
        body_hash: str | None = None,
    ) -> IngestMemoryResponse:
        """Ingest messages into a project's memory.

        Flow:
        1. Idempotency check (Redis) — return cached response if duplicate,
           raise ``ConflictError`` if the key was used with a different body.
        2. Resolve the session.
        3. Compute content hash for content-level dedup (via IdempotencyService).
        4. Redis fast-path pre-check for dedup (fast-path ONLY — never
           relied on for correctness).
        5. Atomically claim the batch: Lua ``claim_content_hash`` (Redis,
           REPLAY returns the winner's ``job_id`` without inserting) then
           the ``ingest_dedup`` unique claim (PostgreSQL, the authoritative
           arbiter inside this transaction).
        6. Build episode dicts from validated messages (server-assigned
            ``sequence_number`` — the request schema carries none).
        7. PII detection & redaction, fail-closed (OpenBao only; fetch or
            redaction failure raises ``PIIUnavailableError`` → 503).
            Runs BEFORE the row lock — network I/O never holds the lock.
            Block-mode ``ValidationError`` downgrades to mask.
        8. Lock the session row (``SELECT ... FOR UPDATE``), take
            ``MAX(sequence_number) + 1``, assign numbers, and batch-insert
            episodes into PostgreSQL (``IntegrityError`` on a lost seq
            race → ``ConflictError`` for client retry).
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
            session_external_id: The session external ID. The session must
                already exist — it is never auto-created.
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
                different request body, or a concurrent ingest won the
                sequence-number race (retry the request).
            PIIUnavailableError: If the PII policy cannot be fetched or
                redaction fails — nothing is persisted.
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

        # ── Step 2: Resolve session ──────────────────────────────────────
        session = await self._resolve_session(
            organization_id=org_id,
            project_id=project_id,
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
            winner_job_id, winner_count = _decode_claim_payload(
                existing_job_id, fallback_count=len(messages)
            )
            return IngestMemoryResponse(
                job_id=winner_job_id,
                episode_count=winner_count,
                status="accepted",
                message="Content already ingested; returning existing job_id",
            )

        # ── Step 4: Claim the batch (TOCTOU-safe dedup) ──────────────────
        # job_id is generated before the claims so the accepted ingest can
        # be referenced by the Redis claim, the dedup row, and the ARQ
        # enrichment tasks.  REPLAY (Redis Lua claim lost, or DB claim
        # lost to a concurrent identical submission) returns the winner's
        # job_id without inserting anything.
        # note: No episodes.content_hash column — the batch-level
        # ingest_dedup unique claim below is already the DB arbiter; a
        # per-episode UNIQUE(org_id, content_hash) on a batch hash would
        # reject every multi-episode batch on its second row.
        job_id = uuid4()
        claim = await self._idem.claim_content_hash(
            str(org_id),
            str(created_by),
            str(session_id),
            msgs,
            payload=_encode_claim_payload(job_id, len(messages)),
        )
        if not claim.won:
            logger.info(
                "memory.content_dedup_hit",
                extra={
                    "content_hash": content_hash[:16] + "...",
                    "existing_job_id": claim.winner,
                    "project_id": str(project_id),
                },
            )
            winner_job_id, winner_count = _decode_claim_payload(
                claim.winner, fallback_count=len(messages)
            )
            return IngestMemoryResponse(
                job_id=winner_job_id,
                episode_count=winner_count,
                status="accepted",
                message="Content already ingested; returning existing job_id",
            )
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
            # The DB row stores only the winner's job_id (no count) — but
            # the losing claim lost on the same content_hash, which covers
            # the full message list, so cardinalities are identical and
            # len(messages) IS the winner's episode_count here.
            return IngestMemoryResponse(
                job_id=str(prior_job_id) if prior_job_id else None,
                episode_count=len(messages),
                status="accepted",
                message="Content already ingested; returning existing job_id",
            )

        # ── Step 5: Build episode dicts (no sequence numbers yet) ─────────
        # Sequence numbers are assigned AFTER the row lock below, so the
        # lock is held only across MAX+1 + INSERT — never across network I/O.
        episode_dicts = [
            {
                "role": msg.role,
                "content": msg.content,
                "metadata": msg.metadata,
                "created_at": msg.created_at,
            }
            for msg in messages
        ]

        # ── Step 6: PII detection & redaction (fail-closed, BEFORE lock) ──
        # OpenBao is network I/O with unbounded latency — fetching it
        # while holding SELECT ... FOR UPDATE would serialize every
        # concurrent ingest behind the slowest PII fetch.
        pii_config_raw = await self._get_org_pii_config(org_id)
        pii_mode = (
            pii_config_raw.get("mode", "mask")
            if isinstance(pii_config_raw, dict)
            else "mask"
        )

        if pii_mode != "off":
            trace_id = structlog.contextvars.get_contextvars().get(
                "request_id", str(job_id)
            )
            for msg_dict in episode_dicts:
                msg_dict["content"] = await self._redact_content(
                    pii_config_raw,
                    msg_dict["content"],
                    org_id=org_id,
                    trace_id=trace_id,
                )

        # ── Step 7: Lock the session row, assign seq numbers ───────────────
        # The FOR UPDATE lock serializes concurrent ingests into this
        # session; it is held only across MAX(sequence_number)+1, seq
        # assignment, and the batch INSERT below (all local work + one
        # round-trip — no network I/O under lock).
        await self._session_repo.get_by_id_for_update(session_id)
        start_seq = await self._episode_repo.get_next_sequence(session_id)
        for i, msg_dict in enumerate(episode_dicts):
            msg_dict["sequence_number"] = start_seq + i

        # ── Step 8: Batch-insert episodes ────────────────────────────────
        try:
            episodes = await self._episode_repo.batch_create(
                organization_id=org_id,
                session_id=session_id,
                project_id=project_id,
                user_id=created_by,
                messages=episode_dicts,
            )
        except IntegrityError as exc:
            # Lost the seq race despite the row lock (or a colliding
            # legacy row) — the unique index held; retry the request.
            logger.warning(
                "memory.sequence_conflict",
                extra={
                    "session_id": str(session_id),
                    "project_id": str(project_id),
                    "org_id": str(org_id),
                    "job_id": str(job_id),
                },
            )
            raise ConflictError(
                "Sequence conflict during ingest — retry the request"
            ) from exc
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
        session_external_id: str,
    ) -> Session:
        """Resolve an existing session by its external ID.

        Look up the existing session and raise ``NotFoundError`` if it
        does not exist.  Sessions are NOT auto-created from arbitrary
        IDs — the SDK must call ``POST /sessions`` first.

        Args:
            organization_id: The organization UUID.
            project_id: The project UUID.
            session_external_id: The caller-defined session identifier.

        Returns:
            A ``Session`` ORM instance.

        Raises:
            NotFoundError: If no session with the given identifier exists.
        """
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

    # ── Idempotency & content dedup ──────────────────────────────────────────
    # Delegated to IdempotencyService (self._idem) — see idempotency_service.py.

    # ── PII Config ────────────────────────────────────────────────────────────

    async def _get_org_pii_config(self, org_id: UUID) -> dict:
        """Fetch PII configuration for an org — OpenBao only, fail-closed.

        Any fetch failure raises ``PIIUnavailableError`` (→ 503 +
        ``Retry-After``) instead of falling back to a default: persisting
        content without a known redaction policy is worse than rejecting
        the request.  The legacy ``organizations.quotas -> 'pii'`` fallback
        was deleted (BREAKING for orgs that still store PII in quotas —
        migrate them to OpenBao org config).

        A successful fetch with ``pii_mode`` unset means "not configured",
        which keeps the previous effective default of ``mask``.

        Args:
            org_id: The organization UUID.

        Returns:
            The PII config dict.

        Raises:
            PIIUnavailableError: If the config cannot be fetched.
        """
        trace_id = structlog.contextvars.get_contextvars().get("request_id", "unknown")
        try:
            from core.org_config import get_org_config

            if self._bao_client is not None:
                org_cfg = await get_org_config(
                    org_id, redis=None, bao_client=self._bao_client
                )
            else:
                # Lazy temporary client — mirrors _process_blobs pattern.
                from core.config import BootstrapSettings
                from core.openbao import OpenBaoClient

                bootstrap = BootstrapSettings()
                async with OpenBaoClient(
                    bootstrap.OPENBAO_ADDR,
                    bootstrap.OPENBAO_ROLE_ID,
                    bootstrap.OPENBAO_SECRET_ID,
                    timeout=10.0,
                ) as _tmp_bao:
                    org_cfg = await get_org_config(
                        org_id, redis=None, bao_client=_tmp_bao
                    )
        except Exception as exc:
            logger.error(
                "pii.fail_closed",
                extra={"org_id": str(org_id), "trace_id": trace_id},
                exc_info=True,
            )
            raise PIIUnavailableError("pii_unavailable") from exc

        if org_cfg is None:
            logger.error(
                "pii.fail_closed",
                extra={"org_id": str(org_id), "trace_id": trace_id},
            )
            raise PIIUnavailableError("pii_unavailable")

        if org_cfg.pii_mode is None:
            return {"mode": "mask"}

        pii: dict[str, Any] = {"mode": org_cfg.pii_mode}
        if org_cfg.pii_sensitivity is not None:
            pii["sensitivity"] = org_cfg.pii_sensitivity
        if org_cfg.pii_enabled_types is not None:
            pii["enabled_types"] = org_cfg.pii_enabled_types
        if org_cfg.pii_min_confidence is not None:
            pii["min_confidence"] = org_cfg.pii_min_confidence
        return pii

    async def _redact_content(
        self,
        pii_config: dict[str, Any],
        content: str,
        *,
        org_id: UUID,
        trace_id: str,
    ) -> str:
        """Redact PII from message content, fail-closed.

        A block-mode ``ValidationError`` downgrades to a single mask pass
        (preserved semantic — the message is still stored, redacted).  Any
        other redaction failure raises ``PIIUnavailableError``: the message
        is never persisted unredacted.  Detections and content are never
        logged — only org/trace identifiers.

        Args:
            pii_config: The PII config dict from :meth:`_get_org_pii_config`.
            content: The raw message content.
            org_id: The organization UUID (log context only).
            trace_id: The request trace ID (log context only).

        Returns:
            The redacted content (unchanged when nothing was detected).

        Raises:
            PIIUnavailableError: If redaction fails unexpectedly.
        """
        from services.pii_service import PIIService

        try:
            try:
                service = PIIService(pii_config)
                redacted, _, _ = await service.process_message(content)
            except ValidationError:
                mask_service = PIIService({**pii_config, "mode": "mask"})
                redacted, _, _ = await mask_service.process_message(content)
                logger.info(
                    "memory.pii_blocked_redacted",
                    extra={"org_id": str(org_id), "trace_id": trace_id},
                )
            return redacted
        except PIIUnavailableError:
            raise
        except Exception as exc:
            logger.error(
                "pii.fail_closed",
                extra={"org_id": str(org_id), "trace_id": trace_id},
            )
            raise PIIUnavailableError("pii_unavailable") from exc

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
                    org_id,
                    redis=None,
                    bao_client=_tmp_bao,
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
