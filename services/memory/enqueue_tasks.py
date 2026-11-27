"""ARQ enqueue helpers — idempotent per-episode task fan-out."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID, uuid4

import structlog

from core.config import get_settings
from core.events import EventType
from core.queue_names import get_queue_name
from schemas.memory import IngestMemoryResponse

logger = logging.getLogger(__name__)

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


class EnqueueTasksMixin:
    """Idempotent ARQ fan-out plus post-commit finalization."""

    _db: Any
    _redis: Any
    _webhook_service: Any
    _idem: Any

    _ENQUEUE_DEDUP_TTL_SEC: int = 7 * 24 * 3600
    """TTL for the enqueue dedupe set — covers the ARQ retry window."""

    async def _enqueue_arq_tasks(
        self,
        job_id: str,
        user_id: str,
        project_id: str,
        session_id: str,
        episodes: list[dict[str, Any]],
        enrichments: dict[str, bool] | None = None,
        enrichment_gate: dict[str, bool | float | str] | None = None,
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

        Enqueues for all episodes concurrently via ``asyncio.gather`` —
        a sequential loop turns ingest latency into
        ``3 * N_episodes`` Redis round-trips.  Only IDs cross the queue;
        workers load content/metadata from the episode row.

        If the ARQ pool is unavailable (Redis down), episodes are safe in
        PostgreSQL and will be picked up by a reconciliation worker.

        Args:
            job_id: The composite job ID for this ingestion.
            user_id: The user UUID string.
            project_id: The project UUID string.
            session_id: The session UUID string.
            episodes: List of episode dicts with ``id`` and ``role``.
            enrichments: Effective per-episode toggles (forwarded into
                every ARQ payload; workers mark-skipped disabled bits).
            enrichment_gate: Effective gate config (forwarded into the
                ``enrich_episode`` ARQ payload only).
        """
        episode_ids = [ep["id"] for ep in episodes]
        trace_id = structlog.contextvars.get_contextvars().get(
            "request_id", str(uuid4())
        )
        from services import memory_service as _facade  # via facade (patchable)
        try:
            arq_pool = _facade.get_arq()
            env = get_settings().ENVIRONMENT
            qname_high = get_queue_name(env, "high")
            qname_low = get_queue_name(env, "low")
            await asyncio.gather(
                *(
                    self._enqueue_one_episode(
                        arq_pool,
                        job_id,
                        user_id,
                        project_id,
                        session_id,
                        trace_id,
                        qname_high,
                        qname_low,
                        str(episode["id"]),
                        episode.get("role", "user"),
                        enrichments,
                        enrichment_gate,
                    )
                    for episode in episodes
                )
            )

            logger.info(
                "memory.arq_tasks_enqueued",
                extra={
                    "job_id": job_id,
                    "task_count": 3,
                    "user_id": user_id,
                    "project_id": project_id,
                },
            )
        except Exception:
            logger.critical(
                "memory.arq_enqueue_failed",
                extra={
                    "job_id": job_id,
                    "user_id": user_id,
                    "project_id": project_id,
                    "session_id": session_id,
                    "episode_ids": episode_ids,
                    "error": "ARQ pool unavailable — tasks not enqueued. "
                    "Episodes are safe in PostgreSQL; reconciliation needed.",
                },
            )
            # Propagate — the post-commit caller logs loudly and keeps
            # the 202 (the reconciler replays the missing work).
            raise

    async def _invalidate_context_cache(
        self, user_id: str, project_id: str
    ) -> None:
        """Invalidate all context cache entries for a project.

        O(1) generation bump: every context key carries ``v{generation}``
        (see :meth:`CacheService.build_context_cache_key`), so bumping
        the ``ctxver:{org}:{project}`` counter retires all pre-ingest
        entries at once — no SCAN+DEL loop.  Stale generations expire
        via TTL.

        Called after ingestion so subsequent context-assembly queries
        fetch fresh data from the database.  The caller treats failures
        as best-effort (logged, never raised).

        Args:
            user_id: The user UUID string.
            project_id: The project UUID string.
        """
        from services.cache_service import CacheService

        key = CacheService.build_context_version_key(user_id, project_id)
        new_version: int = await self._redis.incr(key)
        logger.debug(
            "memory.context_cache_invalidated",
            extra={
                "user_id": user_id,
                "project_id": project_id,
                "new_version": new_version,
            },
        )

    async def _finalize_ingest(
        self,
        *,
        job_id: UUID,
        user_id: UUID,
        project_id: UUID,
        session_id: UUID,
        episodes: list[Any],
        blob_records: list[Any],
        blob_count: int,
        idempotency_key: str | None,
        body_hash: str | None,
        enrichments: dict[str, bool] | None = None,
        enrichment_gate: dict[str, bool | float | str] | None = None,
    ) -> IngestMemoryResponse:
        """Run post-commit fan-out and build the 202 response.

        Enqueues ARQ enrichment + blob extraction, stores the idempotency
        key, purges the context cache, and emits webhooks. Every step is
        best-effort — a failure is logged loudly and the 202 stands.

        Args:
            job_id: The claimed job ID for this ingestion.
            user_id: Tenant scope.
            project_id: Project scope.
            session_id: Target session.
            episodes: Inserted ``Episode`` rows.
            blob_records: Persisted ``EpisodeBlob`` rows.
            blob_count: Number of persisted blob records.
            idempotency_key: Optional client idempotency key.
            body_hash: Optional canonical body hash.
            enrichments: Effective per-episode toggles (forwarded into
                every ARQ payload).
            enrichment_gate: Effective gate config (forwarded into the
                ``enrich_episode`` ARQ payload only).

        Returns:
            The accepted ``IngestMemoryResponse``.
        """
        # ── Enqueue ARQ tasks with the claimed job_id ───────────────────
        # Post-commit: episodes are durable, so an enqueue failure logs
        # loudly and the 202 stands — the reconciler replays the missing
        # work.  Raising here would turn a committed ingest into a 500
        # without un-ingesting anything.
        # ID-only payloads — workers load content/metadata from the row.
        episode_dicts = [{"id": ep.id, "role": ep.role} for ep in episodes]
        degraded = False
        degrade_reason: str | None = None
        try:
            await self._enqueue_arq_tasks(
                job_id=str(job_id),
                user_id=str(user_id),
                project_id=str(project_id),
                session_id=str(session_id),
                episodes=episode_dicts,
                enrichments=enrichments,
                enrichment_gate=enrichment_gate,
            )
        except Exception:
            logger.critical(
                "memory.arq_enqueue_failed_post_commit",
                extra={
                    "job_id": str(job_id),
                    "user_id": str(user_id),
                    "project_id": str(project_id),
                    "session_id": str(session_id),
                    "episode_count": len(episode_dicts),
                },
                exc_info=True,
            )
            degraded = True
            degrade_reason = "enrichment_enqueue_failed"

        if blob_records:
            if not await self._enqueue_blob_extraction_tasks(
                blob_records=blob_records,
                user_id=user_id,
                project_id=project_id,
                enrichments=enrichments,
            ):
                degraded = True
                degrade_reason = "blob_extraction_enqueue_failed"

        # ── Store idempotency key (best-effort) ──────────────────────────
        response = IngestMemoryResponse(
            job_id=str(job_id),
            episode_count=len(episodes),
            blob_count=blob_count,
            status="accepted",
            message="Messages accepted for processing",
            degraded=degraded,
            degrade_reason=degrade_reason,
        )
        if idempotency_key is not None:
            try:
                await self._idem.store_idempotency_key(
                    idempotency_key, body_hash or "", response.model_dump(), str(user_id)
                )
            except Exception:
                # note: no release_content_hash — the claim stays held so
                # the retry replays this attempt's job_id instead of
                # re-ingesting.
                logger.exception(
                    "memory.idempotency_key_store_failed",
                    extra={
                        "user_id": str(user_id),
                        "project_id": str(project_id),
                        "job_id": str(job_id),
                    },
                )

        # note: no store_content_hash here — the Step 4 Lua claim already
        # SET the Redis key atomically on win. A post-ingest
        # check-then-store would re-open the TOCTOU window.

        # ── Post-commit effects (best-effort) ────────────────────────────
        try:
            await self._invalidate_context_cache(
                str(user_id), str(project_id)
            )
        except Exception:
            logger.exception(
                "memory.context_cache_invalidate_failed",
                extra={"user_id": str(user_id), "project_id": str(project_id)},
            )
        if self._webhook_service:
            event_payload = {
                "user_id": str(user_id),
                "project_id": str(project_id),
                "session_id": str(session_id),
                "episode_count": len(episodes),
                "job_id": str(job_id),
            }
            await self._webhook_service.emit(
                user_id=user_id,
                event_type=EventType.INGEST_BATCH_COMPLETED,
                payload=event_payload,
            )
            await self._webhook_service.emit(
                user_id=user_id,
                event_type=EventType.MESSAGE_ADDED,
                payload=event_payload,
            )

        return response

