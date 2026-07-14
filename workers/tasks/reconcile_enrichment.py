"""Enrichment reconciliation — detects stale episodes and re-enqueues missing tasks.

Runs as a periodic ARQ job every 5 minutes.  Queries episodes where
``enrichment_status != ENRICHMENT_ALL`` and that were created or last updated
more than 10 minutes ago (skipping episodes still in-flight).  For each stale
episode, checks which enrichment bits are missing and re-enqueues only the
missing tasks on the high-priority queue.

This is the safety net for worker crashes, job timeouts, or any scenario where
enrichment tasks are dropped without completion.  Without this, a worker crash
leaves episodes un-enriched until an operator manually intervenes.
"""

from __future__ import annotations

import structlog
from datetime import datetime, timedelta, timezone
from typing import Any

from workers.tasks.base import (
    ENRICHMENT_ALL,
    ENRICHMENT_CLASSIFICATION,
    ENRICHMENT_EMBEDDING,
    ENRICHMENT_ENTITIES,
    ENRICHMENT_ENTITY_LINKS,
    ENRICHMENT_FACTS,
    ENRICHMENT_STRUCTURED_EXTRACTION,
)

# Combined LLM enrichment bits — one task replaces 4 individual LLM calls
LLM_ENRICHMENT_BITS: int = (
    ENRICHMENT_ENTITIES
    | ENRICHMENT_FACTS
    | ENRICHMENT_CLASSIFICATION
    | ENRICHMENT_STRUCTURED_EXTRACTION
)

logger = structlog.get_logger()

# ── Constants ──────────────────────────────────────────────────────────────────

RECONCILE_BATCH_SIZE: int = 100
"""Maximum number of stale episodes to process per reconciliation tick.
Limits the burst of re-enqueued jobs on each run."""

STALE_AFTER_MINUTES: int = 30
"""Episodes created/updated within this many minutes are considered
'in-flight' and are skipped by reconciliation."""

BACKLOG_SKIP_THRESHOLD: int = 1_000
"""If the high-priority queue already has more pending jobs than this
threshold, skip reconciliation entirely.  Prevents adding jobs faster
than workers can drain them when there's already a large backlog."""

# ── Task configuration ─────────────────────────────────────────────────────────

# Non-LLM tasks (individual bits, each enqueued separately).
_NON_LLM_TASK_MAP: dict[int, tuple[str, set[str], str]] = {
    ENRICHMENT_EMBEDDING: (
        "embed_episode",
        {"episode_id", "org_id", "project_id", "content", "trace_id", "metadata"},
        "high",
    ),
    ENRICHMENT_ENTITY_LINKS: (
        "link_entities_to_episode",
        {"episode_id", "org_id", "project_id", "content", "role", "trace_id", "metadata"},
        "low",
    ),
}

# Combined LLM task — handles all 4 LLM enrichment bits in one job.
_LLM_TASK_DETAILS: tuple[str, set[str], str] = (
    "enrich_episode",
    {
        "episode_id", "org_id", "project_id", "content",
        "session_id", "trace_id", "metadata", "role",
    },
    "high",
)


async def reconcile_enrichment(ctx: dict[str, Any]) -> str:
    """Detect stale episodes and re-enqueue missing enrichment tasks.

    Queries episodes where ``enrichment_status != ENRICHMENT_ALL`` and
    ``updated_at < NOW() - INTERVAL '{STALE_AFTER_MINUTES} minutes'``.
    For each, checks the current bitmask, computes missing bits, and enqueues
    the corresponding ARQ tasks on the high-priority queue.

    Runs every 5 minutes as an ARQ cron job.  Self-limiting to
    ``RECONCILE_BATCH_SIZE`` (100) episodes per tick to avoid enqueue bursts.

    Args:
        ctx: ARQ worker context dict containing ``db_session_factory`` and
            ``redis`` (an :class:`ArqRedis` instance).

    Returns:
        A summary string for the cron log, e.g.
        ``"Re-enqueued 12 enrichment tasks across 5 episodes"``
        or ``"No stale episodes found"``.

    Raises:
        Exception: If the DB query fails (will be logged by ARQ cron).
    """
    # ── Resolve dependencies from ARQ context ────────────────────────────
    session_factory = ctx.get("db_session_factory")
    if session_factory is None:
        logger.error("reconcile_enrichment.no_session_factory")
        return "Skipped: no db_session_factory in ARQ ctx"

    arq_redis: Any = ctx.get("redis")
    if arq_redis is None:
        logger.error("reconcile_enrichment.no_arq_redis")
        return "Skipped: no redis in ARQ ctx"

    queue_name: str | None = ctx.get("_queue_name")
    if queue_name is None:
        # Fallback: use the low queue (where this cron runs).
        queue_name = "OpenZync:development:queue:low"

    # ── Backlog guard: skip if high-priority queue is already deep ────────
    # Derive the high queue name from the low queue name by replacing suffix.
    high_queue_name = queue_name.replace(":low", ":high")
    try:
        high_depth = await arq_redis.zcard(high_queue_name)
    except Exception:
        high_depth = 0

    if high_depth is not None and high_depth > BACKLOG_SKIP_THRESHOLD:
        logger.info(
            "reconcile_enrichment.skipping_backlog",
            high_depth=high_depth,
            threshold=BACKLOG_SKIP_THRESHOLD,
        )
        return (
            f"Skipped: high-priority queue has {high_depth} pending jobs "
            f"(threshold {BACKLOG_SKIP_THRESHOLD})"
        )

    # ── Query stale episodes ─────────────────────────────────────────────
    from sqlalchemy import select

    from models.episode import Episode

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_AFTER_MINUTES)

    stale_episodes: list[dict[str, Any]] = []

    async with session_factory() as db:
        result = await db.execute(
            select(
                Episode.id,
                Episode.content,
                Episode.organization_id,
                Episode.project_id,
                Episode.session_id,
                Episode.metadata_,
                Episode.enrichment_status,
            ).where(
                Episode.enrichment_status != ENRICHMENT_ALL,
                Episode.updated_at < cutoff,
            )
            .order_by(Episode.updated_at.asc())
            .limit(RECONCILE_BATCH_SIZE)
        )
        rows = result.all()

        for row in rows:
            stale_episodes.append({
                "id": str(row.id),
                "content": row.content,
                "org_id": str(row.organization_id),
                "project_id": str(row.project_id),
                "session_id": str(row.session_id),
                "metadata": row.metadata_,
                "enrichment_status": row.enrichment_status,
            })

    if not stale_episodes:
        logger.debug("reconcile_enrichment.nothing_stale")
        return "No stale episodes found"

    logger.info(
        "reconcile_enrichment.found_stale",
        count=len(stale_episodes),
    )

    # ── Re-enqueue missing tasks ─────────────────────────────────────────
    total_enqueued: int = 0
    episodes_touched: int = 0

    for ep in stale_episodes:
        current_status: int = ep["enrichment_status"]
        org_id: str = ep["org_id"]
        episode_id: str = ep["id"]
        content: str | None = ep.get("content")
        project_id: str = ep.get("project_id", "")

        # Build superset of all possible kwargs from the DB row
        base_kwargs: dict[str, Any] = {
            "episode_id": episode_id,
            "org_id": org_id,
            "project_id": project_id,
            "session_id": ep.get("session_id", ""),
            "metadata": ep.get("metadata", {}),
            "trace_id": f"reconcile_{episode_id[:8]}",
        }
        if content is not None:
            base_kwargs["content"] = content

        # ── Check combined LLM enrichment ────────────────────────────
        # If ANY of the 4 LLM bits are missing, enqueue enrich_episode once.
        if (current_status & LLM_ENRICHMENT_BITS) != LLM_ENRICHMENT_BITS:
            task_name, fields, queue_label = _LLM_TASK_DETAILS
            task_kwargs = {k: v for k, v in base_kwargs.items() if k in fields}
            if "role" in fields and "role" not in task_kwargs:
                task_kwargs["role"] = "user"
            target_queue = queue_name if queue_label == "low" else high_queue_name

            try:
                await arq_redis.enqueue_job(
                    task_name,
                    **task_kwargs,
                    _queue_name=target_queue,
                )
                total_enqueued += 1
            except Exception as exc:
                logger.warning(
                    "reconcile_enrichment.enqueue_failed",
                    task=task_name,
                    episode_id=episode_id,
                    error=str(exc),
                )

        # ── Check individual non-LLM bits ────────────────────────────
        for bit, (task_name, kwarg_set, queue_label) in _NON_LLM_TASK_MAP.items():
            if current_status & bit:
                continue  # bit already set — nothing to do

            task_kwargs = {k: v for k, v in base_kwargs.items() if k in kwarg_set}
            if "role" in kwarg_set and "role" not in task_kwargs:
                task_kwargs["role"] = "user"
            target_queue = queue_name if queue_label == "low" else high_queue_name

            try:
                await arq_redis.enqueue_job(
                    task_name,
                    **task_kwargs,
                    _queue_name=target_queue,
                )
                total_enqueued += 1
            except Exception as exc:
                logger.warning(
                    "reconcile_enrichment.enqueue_failed",
                    task=task_name,
                    episode_id=episode_id,
                    error=str(exc),
                )

        episodes_touched += 1

    summary = (
        f"Re-enqueued {total_enqueued} enrichment tasks "
        f"across {episodes_touched} episodes"
    )
    logger.info("reconcile_enrichment.completed", summary=summary)
    return summary
