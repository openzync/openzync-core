"""Deferred graph-topology observations pass (bit 6).

Runs as a low-priority ARQ worker task triggered after
``link_entities_to_episode`` (bit 3) completes.  Detects co-occurrence,
temporal, and behavioral patterns across all project graph data, generates
natural-language descriptions, and persists them to ``graph_observations``.

Bitmask:
    Checks ``episodes.enrichment_status`` bit 6 before running (idempotent).
    Sets bit 6 on the triggering episode after completion.

Architecture:
    Pattern detection is SQL-first.  All detection algorithms query PostgreSQL
    directly.  The LLM is used **only** to generate the natural-language
    ``content`` field; when unavailable, template-based descriptions are used.

Idempotency:
    Two layers:
    1. Per-episode bit 6 check (inside this worker).
    2. Per-project no-TTL pending marker
       (``observations:pending:{org_id}:{project_id}``), set by
       ``link_entities_to_episode`` on enqueue and deleted by this worker at
       the start of each run — one full-scan run clears the need.
"""

from __future__ import annotations

import structlog

from core.exceptions import EpisodeNotFoundError
from workers.tasks.base import ENRICHMENT_OBSERVATIONS, with_retry

logger = structlog.get_logger()


@with_retry(max_retries=3, base_delay_s=2.0)
async def compute_observations(
    ctx: object,
    episode_id: str,
    org_id: str,
    project_id: str,
    trace_id: str = "",
) -> None:
    """Run graph-topology observations pass for a project.

    Triggered after ``link_entities_to_episode`` sets bit 3.  Performs
    a full project scan of graph topology data and persists observations
    to ``graph_observations``.

    Pipeline:
        1. Set RLS context (``app.org_id``) for org-scoped DB work.
        2. Clear the per-project pending-observations marker.
        3. Resolve the graph backend — skip cleanly if disabled for the org.
        4. Check bit 6 — skip if already set (idempotent).
        5. Bootstrap DB engine from worker context.
        6. Instantiate ``ObservationService``.
        7. Run all detection algorithms (co-occurrence, temporal gaps,
           behavioral patterns).
        8. Optionally call LLM to generate ``content`` field.
        9. Persist observations via ``backend.upsert_observation()``.
        10. Set ``episodes.enrichment_status`` bit 6.
        11. Commit.

    Args:
        ctx: ARQ worker context (provides ``db_engine``, ``redis``).
        episode_id: UUID of the triggering episode.
        org_id: UUID of the owning organization.
        project_id: UUID of the project for project scoping.
        trace_id: Request trace ID for end-to-end correlation.

    Raises:
        Exception: Re-raises after retry exhaustion (``on_exhaustion="raise"``).
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    # ═══════════════════════════════════════════════════════════════════════════
    # Lazy imports — ARQ workers run in a separate process; keep module-level
    # imports lightweight so the worker process starts quickly.
    # ═══════════════════════════════════════════════════════════════════════════
    from uuid import UUID

    from sqlalchemy import text

    from core.config import settings
    from core.db import get_async_session
    from repositories.episode_repository import EpisodeRepository
    from services.observation_service import ObservationService
    from workers.backend import resolve_graph_backend

    logger.info(
        "compute_observations.started",
        episode_id=episode_id,
        org_id=org_id,
        project_id=project_id,
        trace_id=trace_id,
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # Bootstrap DB engine (shared from worker context or create short-lived)
    # ═══════════════════════════════════════════════════════════════════════════
    engine = ctx.get("db_engine") if isinstance(ctx, dict) else None
    if engine is None:
        from core.db import init_db_engine

        engine = init_db_engine(
            str(settings.DATABASE_URL),
            pool_size=2,
            max_overflow=1,
        )
        _own_engine = True
    else:
        _own_engine = False

    session_factory = ctx.get("db_session_factory") if isinstance(ctx, dict) else None
    if session_factory is None:
        session_factory = get_async_session(engine)

    try:
        async with session_factory() as db:
            # ── 1. Set RLS context ──────────────────────────────────────────
            # All DB work below is org-scoped via the ``app.org_id`` GUC —
            # without it the RLS policies filter out the project's rows and
            # the full scan silently finds nothing.
            await db.execute(
                text("SELECT set_config('app.org_id', :org_id, true)"),
                {"org_id": org_id},
            )

            # ── 2. Clear the pending-observations marker ────────────────────
            # One full-scan run clears the need for everything ingested before
            # it; episodes ingested after this point set a fresh marker via
            # ``link_entities_to_episode`` and trigger a new run.
            marker_key = f"observations:pending:{org_id}:{project_id}"
            redis = ctx.get("redis") if isinstance(ctx, dict) else None
            if redis is not None:
                await redis.delete(marker_key)
            else:
                logger.warning(
                    "compute_observations.redis_marker_not_cleared",
                    org_id=org_id,
                    project_id=project_id,
                )

            # ── 3. Resolve graph backend + instantiate service ──────────────
            episode_repo = EpisodeRepository(db)
            ctx_dict: dict = ctx if isinstance(ctx, dict) else {}
            backend = await resolve_graph_backend(ctx_dict, UUID(org_id), db)
            if backend is None:
                # Graph disabled for this org — skip cleanly. Bit 6 is not
                # set, so a later run can still pick this episode up.
                logger.info(
                    "compute_observations.graph_disabled_skipping",
                    org_id=org_id,
                    project_id=project_id,
                    episode_id=episode_id,
                )
                return
            service = ObservationService(
                graph_backend=backend,
                db=db,
            )

            # ── 4. Idempotency check: skip if bit 6 already set ──────────────
            episode = await episode_repo.get_by_id(UUID(episode_id))
            if episode is None:
                logger.warning(
                    "compute_observations.episode_not_found",
                    episode_id=episode_id,
                )
                raise EpisodeNotFoundError(
                    message=f"Episode {episode_id} not found for observations.",
                    detail={"episode_id": episode_id},
                )

            if episode.enrichment_status & ENRICHMENT_OBSERVATIONS:
                logger.info(
                    "compute_observations.already_done",
                    episode_id=episode_id,
                    enrichment_status=episode.enrichment_status,
                )
                return

            # ── 5. Run project-wide pattern detection ─────────────────────────
            # The service runs a full project scan, not just per-episode.
            # ON CONFLICT DO UPDATE prevents duplicate observations.
            pid = UUID(project_id)
            oid = UUID(org_id)

            # Try to get LLM backend for content generation (optional).
            llm_backend = await _maybe_get_llm_backend(ctx)
            if llm_backend is not None:
                logger.info(
                    "compute_observations.using_llm",
                    project_id=project_id,
                )

            counts = await service.run_full_project_scan(
                project_id=pid,
                organization_id=oid,
                llm_backend=llm_backend,
            )

            # ── 6. Set enrichment_status bit 6 ──────────────────────────────
            await episode_repo.apply_enrichment_bits(
                UUID(episode_id), ENRICHMENT_OBSERVATIONS
            )
            await db.commit()

            logger.info(
                "compute_observations.completed",
                episode_id=episode_id,
                project_id=project_id,
                counts=counts,
                total=sum(counts.values()),
            )

    finally:
        if _own_engine:
            await engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


async def _maybe_get_llm_backend(ctx: object) -> object | None:
    """Resolve the LLM backend for content generation, if available.

    The LLM call is **optional** — if the backend cannot be resolved
    (no API key, network unavailable), the worker falls back to
    template-based descriptions via ``ObservationService.build_*`` methods.

    Args:
        ctx: ARQ worker context dict.

    Returns:
        An LLM backend instance or ``None``.
    """
    try:
        from core.llm import resolve_backend

        return await resolve_backend()
    except Exception:
        logger.debug(
            "compute_observations.llm_unavailable",
            message="LLM not available — using template-based descriptions.",
        )
        return None
