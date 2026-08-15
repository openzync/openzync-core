"""Fact service — business logic for batch fact ingestion.

Handles batch validation, deduplication via content hash, and enqueuing
the embedding worker for each ingested fact.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import orjson
import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis

    from models.fact import Fact
    from packages.graph_backend.interface import GraphBackend

from sqlalchemy.ext.asyncio import AsyncSession

from core.arq import get_arq
from core.config import get_settings
from core.events import EventType
from core.exceptions import NotFoundError
from repositories.fact_repository import FactRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from schemas.facts import FactBatchResponse, FactResponse, FactTriple
from services.webhook_service import WebhookService
from services.worker.worker_settings import get_queue_name

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
        webhook_service: Optional webhook emitter.
        graph_backend_resolver: Optional async callable resolving the
            org's graph backend (``None`` when graph is disabled).  The
            resolved backend powers graph edge expiry on supersession —
            see :class:`GraphEdgeSyncService`.  Resolution failures must
            be surfaced as raises here; ``ingest_facts`` downgrades them
            to a warning so a sync failure never fails a fact commit.
    """

    def __init__(
        self,
        db: AsyncSession,
        redis_client: AsyncRedis,
        fact_repo: FactRepository | None = None,
        user_repo: UserRepository | None = None,
        session_repo: SessionRepository | None = None,
        webhook_service: WebhookService | None = None,
        graph_backend_resolver: (
            Callable[[UUID], Awaitable[GraphBackend | None]] | None
        ) = None,
    ) -> None:
        self._db = db
        self._redis = redis_client
        self._webhook_service = webhook_service
        self._graph_backend_resolver = graph_backend_resolver
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
        session_external_id: str,
    ) -> FactBatchResponse:
        """Ingest a batch of facts for a project.

        Flow:
        1. Compute content hash for batch-level dedup.
        2. Resolve the session (NotFoundError if it does not exist).
        3. Bulk-insert facts via the invalidation service (supersedes any
           conflicting active facts — see ``FactInvalidationService``).
        4. Enqueue ARQ embedding task for each inserted fact.
        5. Return 202 response with job_id.

        Behavior change (ADR-005): conflicting batches no longer raise
        409 — conflicts are resolved by supersession and the batch is
        accepted (202).  ``superseded_count`` reports how many previously
        active facts were replaced.

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID.
            created_by: The authenticated user's UUID (attribution).
            facts: List of validated fact triples.
            session_external_id: The session external ID the facts are
                associated with. The session must already exist — it is
                never auto-created.

        Returns:
            A ``FactBatchResponse`` with job_id, accepted_count and
            superseded_count.

        Raises:
            NotFoundError: If the session does not exist in the project.
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

        # ── Step 2: Resolve session ──────────────────────────────────────
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

        # ── Step 4: Supersession-aware insert ─────────────────────────────
        fact_dicts: list[dict[str, Any]] = [
            {
                "subject": f.subject,
                "predicate": f.predicate,
                "object": f.object,
                "content": f.content or f"{f.subject} {f.predicate} {f.object}",
                "confidence": f.confidence,
                "source_episode_id": None,
            }
            for f in facts
        ]

        from services.cache_service import CacheService
        from services.fact_invalidation_service import (
            PURGE_ONLY_CACHE_TTL,
            FactInvalidationService,
        )

        invalidation = FactInvalidationService(
            db=self._db,
            fact_repo=self._fact_repo,
            webhook_service=self._webhook_service,
            cache_service=(
                CacheService(self._redis, default_ttl=PURGE_ONLY_CACHE_TTL)
                if self._redis is not None
                else None
            ),
            graph_sync=await self._resolve_graph_sync(org_id, project_id),
        )
        result = await invalidation.ingest_with_supersession(
            org_id=org_id,
            project_id=project_id,
            user_id=created_by,
            facts=fact_dicts,
        )
        created = result.created

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
                    "session_id": str(session_id),
                    "fact_count": len(created),
                    "job_id": job_id,
                },
            )

        logger.info(
            "fact_service.facts_ingested",
            extra={
                "job_id": job_id,
                "count": len(created),
                "superseded_count": result.superseded_count,
                "project_id": str(project_id),
                "org_id": str(org_id),
            },
        )

        return FactBatchResponse(
            job_id=job_id,
            accepted_count=len(created),
            status="accepted",
            message=f"{len(created)} facts accepted for processing",
            superseded_count=result.superseded_count,
        )

    async def retract_fact(
        self,
        fact_id: UUID,
        *,
        organization_id: UUID,
        project_id: UUID,
        reason: str | None = None,
        at_time: datetime | None = None,
    ) -> Fact:
        """Hard-retract a fact by setting ``invalid_at`` and record its lineage.

        Scoped to ``(organization_id, project_id)``: a fact from another
        project (even in the same org) is indistinguishable from a missing
        one — ``NotFoundError`` either way, so the fact's existence is not
        leaked across project boundaries.

        Idempotent: a fact already closed (``invalid_at`` or ``valid_to``
        set — superseded or previously retracted) is returned unchanged
        with no second event row and no re-notification.  The transaction
        is owned by the caller's session — the ``set_invalid_at``
        primitive updates the row and the event insert flushes in the same
        transaction; the request-scoped ``get_db`` dependency commits when
        the handler returns, firing the post-commit retraction effects.

        Args:
            fact_id: The fact to retract.
            organization_id: Tenant scope — the fact must belong to this
                organization (defense-in-depth; RLS is org-scoped).
            project_id: Project scope — the fact must belong to this
                project or ``NotFoundError`` is raised.
            reason: Optional human-readable explanation.
            at_time: Retraction instant; defaults to now (UTC).

        Returns:
            The fact with ``invalid_at`` set (unchanged on idempotent
            calls).

        Raises:
            NotFoundError: If no fact with the given ID exists in the
                scoped organization and project.
        """
        if at_time is None:
            at_time = datetime.now(UTC)

        fact = await self._fact_repo.get_by_id(
            fact_id, organization_id=organization_id
        )
        # NotFoundError, not 403 — a cross-project fact must be
        # indistinguishable from a nonexistent one (no existence leak).
        if fact is None or fact.project_id != project_id:
            raise NotFoundError(
                message=f"Fact {fact_id} not found",
                detail={"fact_id": str(fact_id)},
            )

        # Idempotency gate — a closed fact is a 200 no-op: no second
        # event row, no re-notify.
        if fact.invalid_at is not None or fact.valid_to is not None:
            logger.debug(
                "fact_retraction.requested",
                extra={
                    "org_id": str(fact.organization_id),
                    "project_id": str(fact.project_id),
                    "fact_id": str(fact.id),
                    "reason": reason,
                    "idempotent": True,
                },
            )
            return fact

        await self._fact_repo.set_invalid_at(fact.id, at_time)
        # The primitive is an UPDATE, not an attribute set — reload the
        # row so the returned/serialized fact reflects invalid_at.
        await self._db.refresh(fact)

        await self._fact_repo.record_invalidation_event(
            organization_id=fact.organization_id,
            project_id=fact.project_id,
            old_fact_id=fact.id,
            new_fact_id=None,
            kind="retracted",
            reason=reason,
            at_time=at_time,
        )

        from services.cache_service import CacheService
        from services.fact_invalidation_service import (
            PURGE_ONLY_CACHE_TTL,
            FactInvalidationService,
        )

        invalidation = FactInvalidationService(
            db=self._db,
            fact_repo=self._fact_repo,
            webhook_service=self._webhook_service,
            cache_service=(
                CacheService(self._redis, default_ttl=PURGE_ONLY_CACHE_TTL)
                if self._redis is not None
                else None
            ),
            graph_sync=await self._resolve_graph_sync(
                fact.organization_id, fact.project_id
            ),
        )
        invalidation.notify_retraction(
            org_id=fact.organization_id,
            project_id=fact.project_id,
            old_fact=fact,
            at_time=at_time,
        )

        logger.info(
            "fact_retraction.requested",
            extra={
                "org_id": str(fact.organization_id),
                "project_id": str(fact.project_id),
                "fact_id": str(fact.id),
                "reason": reason,
                "idempotent": False,
            },
        )

        return fact

    async def get_fact_history(
        self,
        fact_id: UUID,
        *,
        organization_id: UUID,
        project_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Fetch a fact and its invalidation-lineage events.

        Scoped to ``(organization_id, project_id)`` — same non-leaking
        ``NotFoundError`` behavior as :meth:`retract_fact`.

        Args:
            fact_id: The fact whose lineage to fetch.
            organization_id: Tenant scope — the fact must belong to this
                organization (defense-in-depth; RLS is org-scoped).
            project_id: Project scope — the fact must belong to this
                project or ``NotFoundError`` is raised.
            limit: Maximum events (capped at 200 by the repository).
            offset: Number of events to skip (offset pagination).

        Returns:
            A dict with ``fact`` serialized in the ``FactResponse`` shape
            (same as the list endpoint) and ``events`` — lineage event
            dicts, newest first.

        Raises:
            NotFoundError: If no fact with the given ID exists in the
                scoped organization and project.
        """
        fact = await self._fact_repo.get_by_id(
            fact_id, organization_id=organization_id
        )
        if fact is None or fact.project_id != project_id:
            raise NotFoundError(
                message=f"Fact {fact_id} not found",
                detail={"fact_id": str(fact_id)},
            )

        events = await self._fact_repo.get_fact_history(
            fact_id,
            organization_id=organization_id,
            limit=limit,
            offset=offset,
        )
        return {
            "fact": FactResponse.model_validate(fact),
            "events": events,
        }

    # ── Internal helpers ────────────────────────────────────────────────────────

    async def _resolve_graph_sync(
        self, org_id: UUID, project_id: UUID
    ) -> GraphEdgeSyncService | None:
        """Resolve the org's graph backend and build the edge-sync service.

        Runs lazily, only when an ingest is actually about to supersede.
        A resolution failure (no org config, dispatcher error, external
        service down) NEVER fails the ingest — it is logged as a warning
        and the sync is dropped; the ``reconcile_graph_edges`` cron is
        the documented safety net.  Facts are the source of truth; a sync
        failure must not lose or 500 a fact commit.

        Args:
            org_id: The organization UUID.
            project_id: The project UUID (log context).

        Returns:
            A :class:`GraphEdgeSyncService` bound to the resolved backend,
            or ``None`` when the graph is disabled or resolution failed.
        """
        if self._graph_backend_resolver is None:
            return None
        try:
            backend = await self._graph_backend_resolver(org_id)
        except Exception as exc:
            logger.warning(
                "fact_service.graph_backend_resolve_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "error": str(exc),
                },
            )
            return None
        if backend is None:
            return None
        from services.graph_edge_sync_service import GraphEdgeSyncService

        return GraphEdgeSyncService(backends=[backend])

    @staticmethod
    def _compute_batch_hash(
        project_id: UUID,
        facts: list[FactTriple],
    ) -> str:
        """Compute a SHA-256 hash of (project_id, sorted facts).

        Used for content-level deduplication: identical fact batches from
        different clients produce the same hash and return the same job_id.
        ``content`` is part of the fact identity — two batches with the same
        subject/predicate/object but different content hash differently, so
        newer content is never dropped by the dedup short-circuit.

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
                            # Mirrors the content fallback used at ingest time
                            # (ingest_facts Step 4) so the hash matches what
                            # is actually persisted.
                            "content": (
                                f.content or f"{f.subject} {f.predicate} {f.object}"
                            ).strip(),
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
            qname = get_queue_name(get_settings().ENVIRONMENT, ARQ_QUEUE)

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
