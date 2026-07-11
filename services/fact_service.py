"""Fact service — business logic for batch fact ingestion.

Handles batch validation, deduplication via content hash, and enqueuing
the embedding worker for each ingested fact.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import orjson

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis

from sqlalchemy.ext.asyncio import AsyncSession

from core.arq import get_arq
from core.config import get_settings
from core.events import EventType
from core.exceptions import NotFoundError
from repositories.fact_repository import FactRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from schemas.facts import FactBatchResponse, FactTriple
from services.webhook_service import WebhookService

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

CONTENT_HASH_PREFIX = "fact_contenthash:"
"""Redis key prefix for fact content-dedup hash entries."""

IDEMPOTENCY_TTL = 172800  # 48 hours
"""TTL for idempotency and dedup cache entries (seconds)."""

ARQ_QUEUE = "high"
"""ARQ queue name for fact embedding tasks."""


class FactService:
    """Service layer for batch fact ingestion.

    Args:
        db: An async SQLAlchemy session (request-scoped).
        redis_client: An async Redis client for caching and dedup.
        fact_repo: Repository for fact CRUD.
        user_repo: Repository for user CRUD.
        session_repo: Repository for session CRUD.
    """

    def __init__(
        self,
        db: AsyncSession,
        redis_client: AsyncRedis,
        fact_repo: FactRepository | None = None,
        user_repo: UserRepository | None = None,
        session_repo: SessionRepository | None = None,
        webhook_service: WebhookService | None = None,
    ) -> None:
        self._db = db
        self._redis = redis_client
        self._webhook_service = webhook_service
        self._fact_repo = fact_repo or FactRepository(db)
        self._user_repo = user_repo or UserRepository(db)
        self._session_repo = session_repo or SessionRepository(db)

    # ── Public API ──────────────────────────────────────────────────────────────

    async def ingest_facts(
        self,
        org_id: UUID,
        project_id: UUID,
        created_by: UUID,
        facts: list[FactTriple],
        session_external_id: str | None = None,
    ) -> FactBatchResponse:
        """Ingest a batch of facts for a project.

        Flow:
        1. Compute content hash for batch-level dedup.
        2. Optional: resolve session if session_external_id provided.
        3. Bulk-insert facts into PostgreSQL.
        4. Enqueue ARQ embedding task for each fact.
        5. Return 202 response with job_id.

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID.
            created_by: The authenticated user's UUID (attribution).
            facts: List of validated fact triples.
            session_external_id: Optional session external ID.

        Returns:
            A ``FactBatchResponse`` with job_id and accepted_count.
        """
        # ── Step 1: Content-level dedup check ─────────────────────────────
        content_hash = self._compute_batch_hash(project_id, facts)
        existing_job_id = await self._check_dedup(content_hash)
        if existing_job_id is not None:
            logger.info(
                "fact_service.content_dedup_hit",
                extra={
                    "content_hash": content_hash,
                    "existing_job_id": existing_job_id,
                    "project_id": str(project_id),
                },
            )
            return FactBatchResponse(
                job_id=existing_job_id,
                accepted_count=len(facts),
                status="accepted",
                message="Facts already ingested; returning existing job_id",
            )

        # ── Step 2: Optional session resolution ───────────────────────────
        session_id: UUID | None = None
        if session_external_id is not None:
            session = await self._session_repo.get_by_external_id(
                org_id=org_id,
                project_id=project_id,
                external_id=session_external_id,
            )
            if session is None:
                raise NotFoundError(
                    message=f"Session '{session_external_id}' not found "
                    f"in project {project_id}",
                    detail={
                        "session_external_id": session_external_id,
                        "project_id": str(project_id),
                    },
                )
            session_id = session.id

        # ── Step 3: Early return for empty fact lists ─────────────────────
        if not facts:
            return FactBatchResponse(
                job_id="",
                accepted_count=0,
                status="accepted",
                message="No facts to ingest",
            )

        # ── Step 4: Bulk-insert facts ─────────────────────────────────────
        fact_dicts: list[dict[str, Any]] = []
        for f in facts:
            fact_dicts.append({
                "subject": f.subject,
                "predicate": f.predicate,
                "object": f.object,
                "content": f.content or f"{f.subject} {f.predicate} {f.object}",
                "confidence": f.confidence,
                "source_episode_id": None,
                "valid_from": None,
            })

        created = await self._fact_repo.batch_create(
            organization_id=org_id,
            project_id=project_id,
            user_id=created_by,
            facts=fact_dicts,
        )

        # ── Step 5: Generate job_id and enqueue embedding tasks ───────────
        job_id = str(uuid4())
        fact_ids = [str(fact.id) for fact in created]

        await self._enqueue_embedding_tasks(
            job_id=job_id,
            org_id=str(org_id),
            project_id=str(project_id),
            fact_ids=fact_ids,
        )

        # ── Step 6: Cache content hash for future dedup ───────────────────
        await self._cache_dedup(content_hash, job_id)

        # ── Emit webhook event ────────────────────────────────────────
        if self._webhook_service:
            await self._webhook_service.emit(
                organization_id=org_id,
                event_type=EventType.FACT_EXTRACTED,
                payload={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "session_id": str(session_id) if session_id else None,
                    "fact_count": len(created),
                    "job_id": job_id,
                },
            )

        logger.info(
            "fact_service.facts_ingested",
            extra={
                "job_id": job_id,
                "count": len(created),
                "project_id": str(project_id),
                "org_id": str(org_id),
            },
        )

        return FactBatchResponse(
            job_id=job_id,
            accepted_count=len(created),
            status="accepted",
            message=f"{len(created)} facts accepted for processing",
        )

    # ── Internal helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _compute_batch_hash(
        project_id: UUID,
        facts: list[FactTriple],
    ) -> str:
        """Compute a SHA-256 hash of (project_id, sorted facts).

        Used for content-level deduplication: identical fact batches from
        different clients produce the same hash and return the same job_id.

        Args:
            project_id: The project's UUID.
            facts: The fact triples to hash.

        Returns:
            A hex-encoded SHA-256 digest.
        """
        canonical = orjson.dumps(
            {
                "project_id": str(project_id),
                "facts": sorted(
                    [
                        {
                            "subject": f.subject,
                            "predicate": f.predicate,
                            "object": f.object,
                            "confidence": f.confidence,
                        }
                        for f in facts
                    ],
                    key=lambda x: (x["subject"], x["predicate"], x["object"]),
                ),
            },
            option=orjson.OPT_SORT_KEYS,
        )
        return hashlib.sha256(canonical).hexdigest()

    async def _check_dedup(self, content_hash: str) -> str | None:
        """Check if this exact fact batch has been ingested before.

        Args:
            content_hash: The SHA-256 content hash.

        Returns:
            The existing ``job_id`` if found, or ``None``.
        """
        existing = await self._redis.get(f"{CONTENT_HASH_PREFIX}{content_hash}")
        return existing if existing else None

    async def _cache_dedup(self, content_hash: str, job_id: str) -> None:
        """Cache a content hash to prevent re-ingestion of identical facts.

        Args:
            content_hash: The SHA-256 content hash.
            job_id: The job ID to associate with this content.
        """
        await self._redis.setex(
            f"{CONTENT_HASH_PREFIX}{content_hash}",
            IDEMPOTENCY_TTL,
            job_id,
        )

    async def _enqueue_embedding_tasks(
        self,
        job_id: str,
        org_id: str,
        project_id: str,
        fact_ids: list[str],
    ) -> None:
        """Enqueue ARQ embedding tasks for each ingested fact.

        Args:
            job_id: The composite job ID for this ingestion.
            org_id: The organization UUID string.
            project_id: The project UUID string.
            fact_ids: List of fact UUIDs to embed.
        """
        trace_id = structlog.contextvars.get_contextvars().get(
            "request_id", str(uuid4())
        )
        try:
            arq_pool = get_arq()
            qname = self._arq_queue_name(ARQ_QUEUE)

            for fact_id in fact_ids:
                await arq_pool.enqueue(
                    "embed_fact",
                    queue_name=qname,
                    fact_id=fact_id,
                    org_id=org_id,
                    project_id=project_id,
                    trace_id=trace_id,
                )

            logger.info(
                "fact_service.embedding_tasks_enqueued",
                extra={
                    "job_id": job_id,
                    "task_count": len(fact_ids),
                    "org_id": org_id,
                },
            )
        except Exception:
            logger.critical(
                "fact_service.arq_enqueue_failed",
                extra={
                    "job_id": job_id,
                    "org_id": org_id,
                    "project_id": project_id,
                    "fact_ids": fact_ids,
                    "error": "ARQ pool unavailable — tasks not enqueued. "
                    "Facts are safe in PostgreSQL; reconciliation needed.",
                },
            )
            raise  # Propagate so ARQ retry mechanism handles it


    # ── List by session ──────────────────────────────────────────────────────

    async def list_facts_by_session(
        self,
        organization_id: UUID,
        session_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List non-invalidated facts extracted from a session's messages.

        Args:
            organization_id: Tenant scope.
            session_id: The session to fetch facts for.
            limit: Max results per page (1–200).
            cursor: Opaque base64 cursor from a previous page.

        Returns:
            Tuple of (list of fact dicts, next_cursor or None).
        """
        return await self._fact_repo.list_by_session(
            organization_id=organization_id,
            session_id=session_id,
            limit=limit,
            cursor=cursor,
        )

    @staticmethod
    def _arq_queue_name(queue_type: str) -> str:
        """Build the full ARQ queue name matching the worker's config.

        Args:
            queue_type: Queue type suffix (e.g. ``"high"``, ``"low"``).

        Returns:
            Fully qualified queue name for the current environment.
        """
        env = get_settings().ENVIRONMENT
        return f"OpenZync:{env}:queue:{queue_type}"
