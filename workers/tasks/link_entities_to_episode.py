"""Link entities to episode via the graph backend.

Runs after entity extraction is complete (or alongside it).  Reads the
extracted entities from the graph backend and records entity–episode
links with ``backend.link_entity_to_episode`` (FalkorDB ``MENTIONS`` edges
on ``:Episode`` stub nodes, SurrealDB ``has_entity`` edges).  No
PostgreSQL join-table writes — the ``graph_episode_entities`` table is
legacy and no longer on this path.

Previously this worker created a Graphiti ``EpisodicNode`` for each
episode.  That pattern is replaced by backend-native entity–episode
links, eliminating the need for a separate graph database.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from core.exceptions import (
    EpisodeNotFoundError,
    GraphBackendUnavailableError,
    ValidationError,
)
from core.rls import release_tenant_context
from workers.backend import resolve_graph_backend
from workers.tasks.base import (
    ENRICHMENT_ENTITY_LINKS,
    with_retry,
    with_task_metrics,
)

if TYPE_CHECKING:
    from packages.graph_backend.interface import GraphBackend

logger = structlog.get_logger()


def _is_retryable(exc: Exception) -> bool:
    """Retry everything except permanent input errors.

    A missing episode or an invalid payload will fail identically on
    every attempt — retrying burns ``@with_retry`` attempts without
    healing.  They fail fast; everything else (timeouts, pool pressure,
    transient graph errors) is retried.
    """
    return not isinstance(exc, (EpisodeNotFoundError, ValidationError))


@with_task_metrics
@with_retry(max_retries=3, base_delay_s=2.0, is_retryable=_is_retryable)
async def link_entities_to_episode(
    ctx: object,
    episode_id: str,
    org_id: str,
    project_id: str,
    role: str,
    content: str | None = None,
    trace_id: str = "",
    metadata: dict | None = None,
) -> None:
    """Link entities extracted from this episode via the graph backend.

    PostgreSQL holds the episode row and enrichment bit — if this task
    fails, the episode data is not lost and can be retried.

    Flow:
    1. Bootstrap a temporary DB engine
    2. Get the episode row
    3. Search for entities via ``backend.bulk_search_entities`` by
       name/content match
    4. Link matching entities via ``backend.link_entity_to_episode``
       (backend-native edge, no PG join-table INSERT)
    5. Set enrichment_status bit 3

    Args:
        ctx: ARQ worker context (unused, required by ARQ contract).
        episode_id: UUID of the episode to sync.
        org_id: UUID of the owning organization.
        project_id: UUID of the project for project scoping.
        role: Message role (user/assistant/system/tool).
        content: Legacy payload from older enqueues.  ``None`` (the new
            default) matches entity names against the episode row instead.
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.
        metadata: Optional metadata dict forwarded from the enrichment pipeline.

    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    from sqlalchemy import select

    from core import rls
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
            try:
                await rls.set_tenant_context(db, org_id)
                # ── 1. Get the episode ──────────────────────────────────────────
                result = await db.execute(
                    select(Episode).where(Episode.id == episode_id)
                )
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
                except GraphBackendUnavailableError:
                    # Backend is explicitly configured but unreachable — this is
                    # NOT the "disabled" case. Re-raise so ARQ retries; swallowing
                    # here silently drops entity links for this episode.
                    logger.warning(
                        "link_entities_to_episode.backend_unavailable",
                        org_id=org_id,
                        exc_info=True,
                    )
                    raise
                except Exception:
                    # Unexpected resolver errors must surface too — re-raise so
                    # ARQ retries rather than silently unlinking the episode.
                    logger.warning(
                        "link_entities_to_episode.backend_resolution_failed",
                        org_id=org_id,
                        episode_id=episode_id,
                    )
                    raise
                # Any other exception from the resolver also propagates — no
                # silent swallow that would set the link bit with 0 links.

                # Extract candidate tokens from content (simple keyword split)
                # episode_id-only jobs carry no text — match against the row.
                link_text = content if content is not None else episode.content
                tokens = {
                    w.strip().rstrip(".,!?:;")
                    for w in link_text.split()
                    if len(w.strip()) > 2
                }

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
                    entities_found_per_word: list[int] = []
                    words: set[str] = set()
                else:
                    # ── 3. Match tokens against known entity names ──────────
                    # Case-insensitive: a capitalized-first-letter heuristic
                    # misses lowercase mentions and misfires on
                    # sentence-initial words.  Only tokens matching a known
                    # entity name (or one of its words) are searched.
                    known_tokens: set[str] = set()
                    try:
                        cursor: str | None = None
                        while True:
                            entity_page = await backend.list_entities(
                                org_id=UUID(org_id),
                                project_id=UUID(project_id),
                                limit=200,
                                cursor=cursor,
                            )
                            for item in entity_page.get("items", []):
                                name = (item.get("name") or "").strip()
                                if name:
                                    known_tokens.add(name.lower())
                                    known_tokens.update(name.lower().split())
                            cursor = entity_page.get("next_cursor")
                            if not entity_page.get("has_more"):
                                break
                    except GraphBackendUnavailableError:
                        logger.warning(
                            "link_entities_to_episode.entity_list_failed",
                            org_id=org_id,
                            exc_info=True,
                        )
                        raise
                    words = {t for t in tokens if t.lower() in known_tokens}

                    # ── 4. Search for matching entities (bounded fan-out) ──
                    # One bulk_search per word, capped in flight — a long
                    # episode must not fan out unbounded graph queries.
                    first_error: GraphBackendUnavailableError | None = None
                    words_matched = 0
                    entities_found_per_word = []
                    linked = 0
                    _search_semaphore = asyncio.Semaphore(8)

                    async def _search_one(word: str) -> list[dict]:
                        async with _search_semaphore:
                            return await backend.bulk_search_entities(
                                org_id=UUID(org_id),
                                project_id=UUID(project_id),
                                query=word,
                                limit=5,
                            )

                    search_results = await asyncio.gather(
                        *(_search_one(word) for word in sorted(words)),
                        return_exceptions=True,
                    )
                    matched: list[tuple[str, list[dict]]] = []
                    for word, outcome in zip(
                        sorted(words), search_results, strict=True
                    ):
                        if isinstance(outcome, BaseException):
                            if isinstance(
                                outcome, GraphBackendUnavailableError
                            ):
                                logger.warning(
                                    "link_entities_to_episode.search_failed",
                                    word=word,
                                    exc_info=outcome,
                                )
                                if first_error is None:
                                    first_error = outcome
                                continue
                            raise outcome
                        if outcome:
                            words_matched += 1
                            entities_found_per_word.append(len(outcome))
                            matched.append((word, outcome))
                    for _word, entities in matched:
                        for entity in entities:
                            try:
                                await backend.link_entity_to_episode(
                                    org_id=UUID(org_id),
                                    project_id=UUID(project_id),
                                    episode_id=UUID(episode_id),
                                    entity_id=UUID(entity["id"]),
                                )
                                linked += 1
                            except GraphBackendUnavailableError as exc:
                                logger.warning(
                                    "link_entities_to_episode.link_failed",
                                    entity_id=entity.get("id"),
                                    exc_info=True,
                                )
                                if first_error is None:
                                    first_error = exc
                    if first_error is not None:
                        # Never set bit 3 on partial work — the retry
                        # replays the missing links instead of marking
                        # the episode linked with zero edges.
                        raise first_error

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
                        from services.worker.worker_settings import (
                            settings as w_settings,
                        )

                        # 1h-TTL pending marker, keyed per (org, project).
                        # compute_observations deletes it when its full project
                        # scan runs; episodes ingested after that set a fresh
                        # marker.  No TTL would leak one key per project
                        # forever in the 128MB noeviction Redis; 1h bounds
                        # the leak if the deleter ever misses.
                        marker_key = f"observations:pending:{org_id}:{project_id}"
                        if not await arq_redis.get(marker_key):
                            await arq_redis.enqueue_job(
                                "compute_observations",
                                episode_id=episode_id,
                                org_id=org_id,
                                project_id=project_id,
                                trace_id=trace_id,
                                _queue_name=w_settings.low_queue_full,
                            )
                            # Set only after a successful enqueue — a stale marker
                            # after a failed enqueue would block future triggers.
                            await arq_redis.set(marker_key, "1", ex=3600)
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
                            sum(entities_found_per_word)
                            / len(entities_found_per_word),
                            2,
                        )
                        if entities_found_per_word
                        else 0
                    ),
                )
            finally:
                await release_tenant_context(db)

    finally:
        if _own_engine:
            await engine.dispose()
