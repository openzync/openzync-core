"""Per-episode fan-out — idempotent enqueue of the three enrichment tasks."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PerEpisodeEnqueueMixin:
    """One-episode idempotent ARQ fan-out."""

    _redis: Any

    async def _enqueue_one_episode(
        self,
        arq_pool: Any,
        job_id: str,
        org_id: str,
        project_id: str,
        session_id: str,
        trace_id: str,
        qname_high: str,
        qname_low: str,
        episode_id: str,
        role: str,
    ) -> None:
        """Enqueue the three enrichment tasks for one episode, idempotently.

        Each ``(task, episode)`` pair carries a Redis dedupe key, so a
        retry of the ingest (after a mid-fan-out crash) never
        double-enqueues work — workers themselves skip completed bits,
        but duplicate jobs would still waste LLM calls on re-entry.
        The key is claimed atomically (``SET key token NX EX``) BEFORE the
        enqueue, so two concurrent same-episode ingests cannot double-enqueue
        — only the replica that owns the claim proceeds. On enqueue failure
        the key is released only if it still holds our token (Lua
        compare-and-delete), so a failed enqueue never blocks its retry
        while a concurrent winner's claim is never stolen.

        Workers load content/metadata from the episode row — only IDs
        (plus the small ``role``/``session_id`` routing hints) cross the
        queue, keeping job payloads small.

        Args:
            arq_pool: The ARQ Redis pool.
            job_id: The composite job ID for this ingestion.
            org_id: The organization UUID string.
            project_id: The project UUID string.
            session_id: The session UUID string.
            trace_id: Request trace ID for end-to-end correlation.
            qname_high: High-priority queue name for this environment.
            qname_low: Low-priority queue name for this environment.
            episode_id: The episode UUID string.
            role: Message role (kept — small, and needed before the
                episode row is re-read by workers).
        """
        tasks = (
            ("enrich_episode", qname_high, {"session_id": session_id, "role": role}),
            ("embed_episode", qname_high, {}),
            ("link_entities_to_episode", qname_low, {"role": role}),
        )
        for task_name, qname, extra in tasks:
            dedup_key = f"memory:enqueued:{task_name}:{episode_id}"
            claimed: bool = await self._redis.set(
                dedup_key, job_id, ex=self._ENQUEUE_DEDUP_TTL_SEC, nx=True
            )
            if not claimed:
                logger.info(
                    "memory.arq_task_already_enqueued",
                    extra={
                        "job_id": job_id,
                        "task": task_name,
                        "episode_id": episode_id,
                    },
                )
                continue
            try:
                await arq_pool.enqueue(
                    task_name,
                    queue_name=qname,
                    episode_id=episode_id,
                    org_id=org_id,
                    project_id=project_id,
                    trace_id=trace_id,
                    **extra,
                )
            except Exception:
                # Release only our own claim — a concurrent winner's key is
                # never stolen, and without this a failed enqueue would block
                # its retry until the TTL expires.
                try:
                    await self._redis.eval(
                        "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                        "return redis.call('DEL', KEYS[1]) else return 0 end",
                        1,
                        dedup_key,
                        job_id,
                    )
                except Exception:
                    logger.exception(
                        "memory.enqueue_dedup_cleanup_failed",
                        extra={
                            "job_id": job_id,
                            "task": task_name,
                            "episode_id": episode_id,
                        },
                    )
                raise

