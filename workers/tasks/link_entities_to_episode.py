"""Link entities to episode via graph_episode_entities join table.

Runs after entity extraction is complete (or alongside it).  Reads the
extracted entities from the graph backend and links them to this episode
in the ``graph_episode_entities`` join table.

Previously this worker created a Graphiti ``EpisodicNode`` for each
episode.  That pattern is replaced by storing entity–episode links in
PostgreSQL, eliminating the need for a separate graph database.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from core.exceptions import (
    EpisodeNotFoundError,
    GraphBackendUnavailableError,
)
from packages.graph_backend.interface import GraphBackend
from workers.backend import resolve_graph_backend
from workers.tasks.base import ENRICHMENT_ENTITY_LINKS, with_retry

logger = structlog.get_logger()


@with_retry(max_retries=3, base_delay_s=2.0)
async def link_entities_to_episode(
    ctx: object,
    episode_id: str,
    org_id: str,
    project_id: str,
    content: str,
    role: str,
    trace_id: str = "",
    metadata: dict | None = None,
) -> None:
    """Link entities extracted from this episode via graph_episode_entities.

    PostgreSQL is the authoritative store — if this task fails, the episode
    data is not lost and can be retried.

    Flow:
    1. Bootstrap a temporary DB engine
    2. Get the episode row
    3. Search for entities in graph_entities by name/content match
    4. Link matching entities via INSERT INTO graph_episode_entities
    5. Set enrichment_status bit 3

    Args:
        ctx: ARQ worker context (unused, required by ARQ contract).
        episode_id: UUID of the episode to sync.
        org_id: UUID of the owning organization.
        project_id: UUID of the project for project scoping.
        content: Episode message text (used for entity name matching).
        role: Message role (user/assistant/system/tool).
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.

    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    from sqlalchemy import select

    from core.config import settings
    from core.db import get_async_session
    from models.episode import Episode
    from repositories.episode_repository import EpisodeRepository

    # Use the shared engine from worker context.
    engine = ctx.get("db_engine") if isinstance(ctx, dict) else None
    if engine is None:
        from core.db import init_db_engine

        engine = init_db_engine(
            str(settings.DATABASE_URL),
            pool_size=5,
            max_overflow=2,
        )
        _own_engine = True
    else:
        _own_engine = False
    session_factory = ctx.get("db_session_factory") if isinstance(ctx, dict) else None
    if session_factory is None:
        session_factory = get_async_session(engine)

    try:
        async with session_factory() as db:
            # ── 1. Get the episode ──────────────────────────────────────────
            result = await db.execute(select(Episode).where(Episode.id == episode_id))
            episode = result.scalar_one_or_none()
            if episode is None:
                logger.warning(
                    "link_entities_to_episode.episode_not_found",
                    episode_id=episode_id,
                    org_id=org_id,
                    project_id=project_id,
                )
                raise EpisodeNotFoundError(
                    message=f"Episode {episode_id} not found for entity linking.",
                    detail={"episode_id": episode_id},
                )

            # ── 2. Resolve graph backend ─────────────────────────────────────
            backend: GraphBackend | None
            try:
                ctx_dict: dict = ctx if isinstance(ctx, dict) else {}
                backend = await resolve_graph_backend(
                    ctx_dict, UUID(org_id), db,
                )
            except Exception:
                logger.warning(
                    "link_entities_to_episode.backend_resolution_failed",
                    org_id=org_id,
                    exc_info=True,
                )
                backend = None

            # Extract potential entity names from content (simple keyword split)
            words = set(
                w.strip().rstrip(".,!?:;")
                for w in content.split()
                if len(w.strip()) > 2 and w.strip()[0].isupper()
            )

            if backend is None:
                logger.info(
                    "link_entities_to_episode.graph_disabled_skipping",
                    org_id=org_id,
                    episode_id=episode_id,
                )
                # Skip entity linking — not critical for enrichment pipeline.
                # Episodes can be re-linked later when the graph is available.
                linked = 0
                words_matched = 0
                entities_found_per_word = []
            else:
                # ── 3. Search for matching entities via graph backend ──────────

                words_matched = 0
                entities_found_per_word: list[int] = []
                linked = 0
                for word in words:
                    if not word:
                        continue
                    try:
                        entities = await backend.bulk_search_entities(
                            org_id=UUID(org_id),
                            project_id=UUID(project_id),
                            query=word,
                            limit=5,
                        )
                    except GraphBackendUnavailableError:
                        logger.warning(
                            "link_entities_to_episode.search_failed",
                            word=word,
                            exc_info=True,
                        )
                        continue

                    if entities:
                        words_matched += 1
                        entities_found_per_word.append(len(entities))
                    for entity in entities:
                        try:
                            await backend.link_entity_to_episode(
                                org_id=UUID(org_id),
                                project_id=UUID(project_id),
                                episode_id=UUID(episode_id),
                                entity_id=UUID(entity["id"]),
                            )
                            linked += 1
                        except GraphBackendUnavailableError:
                            logger.warning(
                                "link_entities_to_episode.link_failed",
                                entity_id=entity.get("id"),
                                exc_info=True,
                            )

            # ── 5. Update enrichment_status bit 3 ───────────────────────────
            episode_repo = EpisodeRepository(db)
            await episode_repo.apply_enrichment_bits(
                UUID(episode_id), ENRICHMENT_ENTITY_LINKS
            )
            await db.commit()

            # ── 6. Enqueue deferred observations pass (bit 6) ────────────────
            try:
                arq_redis: object | None = None
                if isinstance(ctx, dict):
                    arq_redis = ctx.get("redis")
                if arq_redis is not None:
                    from services.worker.worker_settings import settings as w_settings

                    dedup_key = f"observations:pending:{project_id}"
                    if not await arq_redis.get(dedup_key):
                        await arq_redis.enqueue_job(
                            "compute_observations",
                            episode_id=episode_id,
                            org_id=org_id,
                            project_id=project_id,
                            trace_id=trace_id,
                            _queue_name=w_settings.low_queue_full,
                        )
                        await arq_redis.set(dedup_key, "1", ex=30)
                        logger.info(
                            "link_entities_to_episode.scheduled_observations",
                            episode_id=episode_id,
                            project_id=project_id,
                        )
            except Exception as exc:
                logger.warning(
                    "link_entities_to_episode.observations_enqueue_failed",
                    extra={"project_id": project_id, "error": str(exc)},
                )
                raise  # Propagate so ARQ retry mechanism handles it

            # ── 7. Optionally trigger community detection (event-driven mode) ──
            from services.worker.worker_settings import settings as worker_settings
            if worker_settings.AUTO_RUN_COMMUNITY_DETECTION:
                try:
                    # ctx is the ARQ worker context dict with a 'redis' key
                    # arq_redis is already declared and populated at line 174
                    if isinstance(ctx, dict):
                        arq_redis = ctx.get("redis")
                    if arq_redis is not None:
                        dedup_key = f"community:recently_enqueued:{org_id}"
                        if not await arq_redis.get(dedup_key):
                            await arq_redis.enqueue_job(
                                "summarise_community",
                                org_id=org_id,
                                _queue_name=worker_settings.low_queue_full,
                            )
                            # Prevent re-enqueueing within 1 hour per org
                            await arq_redis.set(dedup_key, "1", ex=3600)
                            logger.info(
                                "link_entities_to_episode.scheduled_community_detection",
                                org_id=org_id,
                            )
                except Exception as exc:
                    logger.warning(
                        "link_entities_to_episode.community_enqueue_failed",
                        extra={"org_id": org_id, "error": str(exc)},
                    )
                    raise  # Propagate so ARQ retry mechanism handles it

            logger.info(
                "link_entities_to_episode.completed",
                episode_id=episode_id,
                org_id=org_id,
                project_id=project_id,
                entities_linked=linked,
                words_analyzed=len(words),
                words_matched=words_matched,
                avg_entities_per_match=(
                    round(
                        sum(entities_found_per_word) / len(entities_found_per_word), 2
                    )
                    if entities_found_per_word
                    else 0
                ),
            )

    finally:
        if _own_engine:
            await engine.dispose()
