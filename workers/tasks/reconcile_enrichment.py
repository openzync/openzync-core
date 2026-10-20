"""Enrichment reconciliation — detects stale episodes and re-enqueues missing tasks.

Runs as a periodic ARQ job every 5 minutes.  Queries episodes where
the LLM enrichment bits are not all set and that were created or last
updated more than 30 minutes ago (skipping episodes still in-flight).
For each stale episode, checks which enrichment bits are missing and
re-enqueues only the missing tasks on the high-priority queue.

Also runs a separate fact-embedding repair pass: facts with no embedding and
no ``embedded_at`` timestamp (never attempted, not retracted) are re-enqueued
for ``embed_fact``.  Facts whose embedding permanently failed are retired by
``embed_fact`` (``embedded_at`` set) and are excluded.

This is the safety net for worker crashes, job timeouts, or any scenario where
enrichment tasks are dropped without completion.  Without this, a worker crash
leaves episodes un-enriched until an operator manually intervenes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from core import rls
from services.worker.worker_settings import settings
from workers.tasks.base import (
    ENRICHMENT_CLASSIFICATION,
    ENRICHMENT_EMBEDDING,
    ENRICHMENT_ENTITIES,
    ENRICHMENT_ENTITY_LINKS,
    ENRICHMENT_FACTS,
    ENRICHMENT_STRUCTURED_EXTRACTION,
    LLM_INVALIDATION_BIT,
    with_task_metrics,
)

# Combined LLM enrichment bits — one task replaces 4 individual LLM calls
# (plus the LLM-driven invalidation pass inside the same facts savepoint).
LLM_ENRICHMENT_BITS: int = (
    ENRICHMENT_ENTITIES
    | ENRICHMENT_FACTS
    | ENRICHMENT_CLASSIFICATION
    | ENRICHMENT_STRUCTURED_EXTRACTION
    | LLM_INVALIDATION_BIT
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
        {
            "episode_id",
            "org_id",
            "project_id",
            "content",
            "role",
            "trace_id",
            "metadata",
        },
        "low",
    ),
}

# Combined LLM task — handles all 4 LLM enrichment bits in one job.
_LLM_TASK_DETAILS: tuple[str, set[str], str] = (
    "enrich_episode",
    {
        "episode_id",
        "org_id",
        "project_id",
        "content",
        "session_id",
        "trace_id",
        "metadata",
        "role",
    },
    "high",
)


async def _repair_missing_fact_embeddings(
    session_factory: Any,
    arq_redis: Any,
    high_queue_name: str,
) -> int:
    """Enqueue ``embed_fact`` for facts whose embedding never ran.

    Fact embedding state is tracked by ``facts.embedded_at``: facts are
    eligible for repair only when never attempted (``embedding IS NULL AND
    embedded_at IS NULL``) and not retracted (``invalid_at IS NULL``).  Facts
    retired by ``embed_fact`` (permanent 4xx) have ``embedded_at`` set
    and are excluded.  The embedder is a local ONNX model with no
    per-org configuration, so there is no misconfigured-org case to skip.

    Args:
        session_factory: ARQ ctx async session factory.
        arq_redis: ARQ Redis client used to enqueue jobs.
        high_queue_name: Name of the high-priority queue.

    Returns:
        Number of ``embed_fact`` jobs enqueued.
    """
    from sqlalchemy import select

    from models.fact import Fact
    from models.project import Project

    rows: list[dict[str, Any]] = []
    async with session_factory() as db:
        # Cross-org cron scan: bypass RLS so every org is visible.  The
        # policies read NULLIF(current_setting('app.org_id', true), ''),
        # so an unset context matches no rows — the bypass is what makes
        # the platform-wide scan possible.  The bypass is bound on the
        # session, so it survives a commit instead of silently vanishing.
        await rls.set_bypass_rls(db, True)
        result = await db.execute(
            select(
                Fact.id,
                Fact.content,
                Fact.organization_id,
                Fact.project_id,
            )
            .where(
                Fact.embedding.is_(None),
                Fact.embedded_at.is_(None),
                Fact.invalid_at.is_(None),
                Fact.project_id.not_in(
                    select(Project.id).where(Project.is_archived.is_(True))
                ),
            )
            .order_by(Fact.created_at.asc())
            .limit(RECONCILE_BATCH_SIZE)
        )
        for row in result.all():
            rows.append(
                {
                    "id": str(row.id),
                    "content": row.content,
                    "org_id": str(row.organization_id),
                    "project_id": str(row.project_id),
                }
            )

    if not rows:
        return 0

    logger.info(
        "reconcile_enrichment.found_unembedded_facts",
        count=len(rows),
    )

    # Resolve org config once per org, not once per fact.
    by_org: dict[str, list[dict[str, Any]]] = {}
    for fact_row in rows:
        by_org.setdefault(fact_row["org_id"], []).append(fact_row)

    enqueued: int = 0
    for org_id, org_facts in by_org.items():
        for fact_row in org_facts:
            task_kwargs = {
                "fact_id": fact_row["id"],
                "org_id": org_id,
                "project_id": fact_row["project_id"],
                "content": fact_row["content"],
                "trace_id": f"reconcile_{fact_row['id'][:8]}",
            }
            try:
                await arq_redis.enqueue_job(
                    "embed_fact",
                    **task_kwargs,
                    _queue_name=high_queue_name,
                )
                enqueued += 1
            except Exception as exc:
                logger.warning(
                    "reconcile_enrichment.enqueue_failed",
                    task="embed_fact",
                    fact_id=fact_row["id"],
                    error=str(exc),
                )

    return enqueued


@with_task_metrics
async def reconcile_enrichment(ctx: dict[str, Any]) -> str:
    """Detect stale episodes and re-enqueue missing enrichment tasks.

    Queries episodes where ``(enrichment_status & LLM_ENRICHMENT_BITS)
    != LLM_ENRICHMENT_BITS`` and ``updated_at`` is older than
    ``STALE_AFTER_MINUTES``.  For each, checks the current bitmask,
    computes missing bits, and enqueues the corresponding ARQ tasks on
    the high-priority queue.

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

    # Queue targets come from the worker's own settings.  ARQ never puts
    # ``_queue_name`` in the task context, so the previous ctx lookup always
    # fell back to a hard-coded development queue that no production worker
    # consumes — the crash-recovery safety net wrote to a dead queue.
    low_queue_name: str = settings.low_queue_full
    high_queue_name: str = settings.high_queue_full

    # ── Backlog guard: skip if high-priority queue is already deep ────────
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

    # ── Fact-embedding repair pass ──────────────────────────────────────
    # Separate from the episode pass below — facts with a NULL embedding
    # cannot be expressed as an episode enrichment bit.
    fact_embedding_enqueued = await _repair_missing_fact_embeddings(
        session_factory,
        arq_redis,
        high_queue_name,
    )

    # ── Query stale episodes ─────────────────────────────────────────────
    from sqlalchemy import or_, select

    from models.episode import Episode
    from models.project import Project

    cutoff = datetime.now(UTC) - timedelta(minutes=STALE_AFTER_MINUTES)

    stale_episodes: list[dict[str, Any]] = []

    async with session_factory() as db:
        # Cross-org cron scan: bypass RLS so every org is visible (same
        # reason as the fact-embedding pass above).
        await rls.set_bypass_rls(db, True)
        result = await db.execute(
            select(
                Episode.id,
                Episode.content,
                Episode.organization_id,
                Episode.project_id,
                Episode.session_id,
                Episode.metadata_,
                Episode.enrichment_status,
            )
            .where(
                # ⚠️ The EMBEDDING bit is an OR-arm, not part of the mask
                # below: an episode can be fully LLM-enriched and still
                # need re-embedding (a same-dimension model swap clears
                # only this bit — see scripts/reset_embeddings_for_remodel.py).
                # Matching on the LLM mask alone would never select it.
                or_(
                    (Episode.enrichment_status.op("&")(LLM_ENRICHMENT_BITS))
                    != LLM_ENRICHMENT_BITS,
                    Episode.enrichment_status.op("&")(ENRICHMENT_EMBEDDING) == 0,
                ),
                Episode.is_deleted.is_(False),
                Episode.updated_at < cutoff,
                Episode.project_id.not_in(
                    select(Project.id).where(Project.is_archived.is_(True))
                ),
            )
            .order_by(Episode.updated_at.asc())
            .limit(RECONCILE_BATCH_SIZE)
        )
        rows = result.all()

        for row in rows:
            stale_episodes.append(
                {
                    "id": str(row.id),
                    "content": row.content,
                    "org_id": str(row.organization_id),
                    "project_id": str(row.project_id),
                    "session_id": str(row.session_id),
                    "metadata": row.metadata_,
                    "enrichment_status": row.enrichment_status,
                }
            )

    if not stale_episodes:
        logger.debug("reconcile_enrichment.nothing_stale")
        if fact_embedding_enqueued:
            return f"Re-enqueued {fact_embedding_enqueued} fact embedding tasks"
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
            target_queue = low_queue_name if queue_label == "low" else high_queue_name

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
            target_queue = low_queue_name if queue_label == "low" else high_queue_name

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
    if fact_embedding_enqueued:
        summary += f", plus {fact_embedding_enqueued} fact embedding tasks"
    logger.info("reconcile_enrichment.completed", summary=summary)
    return summary
