"""Embedding worker — generates pgvector embeddings for episode content.

Runs after entity extraction (enrichment_status bit 0 must be set).
Generates embeddings with the single local embedder
(``core.embeddings.embed_passage``) and stores them in the
``episodes.embedding`` column.

Queue: high-priority (real-time ingestion).
"""

from __future__ import annotations

import structlog

from core.exceptions import EpisodeNotFoundError
from core.rls import release_tenant_context
from workers.tasks.base import (
    ENRICHMENT_EMBEDDING,
    with_retry,
    with_task_metrics,
)

logger = structlog.get_logger()


@with_task_metrics
@with_retry(max_retries=3, base_delay_s=2.0)
async def embed_episode(
    ctx: object,
    episode_id: str,
    org_id: str,
    project_id: str,
    content: str | None = None,
    trace_id: str = "",
    metadata: dict | None = None,
) -> None:
    """Generate an embedding for an episode and store it in pgvector.

    Embeds with the single local embedder (``core.embeddings.embed_passage``)
    — the document prefix, since this text is written to the corpus side of
    the vector store.  Any vector that is not exactly ``CANONICAL_EMBED_DIM``
    raises ``ExternalServiceError`` and is never stored.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        episode_id: UUID of the episode to embed.
        org_id: UUID of the owning organisation (for observability / RLS).
        project_id: UUID of the project for project scoping (observability).
        content: Legacy payload from older enqueues.  ``None`` (the new
            default) loads the text from the episode row instead.
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.
        metadata: Optional metadata dict forwarded from the enrichment pipeline.

    Raises:
        EpisodeNotFoundError: If no episode exists for ``episode_id``.
        ExternalServiceError: If the embedder returns a non-canonical-dim
            vector.
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    # ── Lazy imports (ARQ workers run in a separate process) ──────────────
    import uuid

    from sqlalchemy import text

    from core import rls
    from core.config import settings
    from core.db import get_async_session
    from core.embeddings import CANONICAL_EMBED_DIM, embed_passage
    from repositories.episode_repository import EpisodeRepository
    from repositories.project_repository import ProjectRepository

    logger.info(
        "embed_episode.started",
        episode_id=episode_id,
        org_id=org_id,
        project_id=project_id,
        trace_id=trace_id,
    )

    # ── 1. Resolve DB engine / session factory ─────────────────────────────
    # Moved up from the DB write section — needed here for org config fetch.
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

    # ── 2a. Idempotency check — skip if embedding bit already set ────────────
    async with session_factory() as idempotency_db:
        try:
            # Bind the session to the tenant before the first org-scoped
            # query — under enforced RLS an unbound session sees no rows.
            await rls.set_tenant_context(idempotency_db, org_id)
            episode_repo = EpisodeRepository(idempotency_db)
            episode = await episode_repo.get_by_id(uuid.UUID(episode_id))
            if episode is None:
                logger.warning(
                    "embed_episode.episode_not_found",
                    episode_id=episode_id,
                )
                raise EpisodeNotFoundError(
                    message=f"Episode {episode_id} not found for embedding.",
                    detail={"episode_id": episode_id},
                )
            # episode_id-only jobs carry no text — load it from the row.
            episode_text = content if content is not None else episode.content
            if episode.enrichment_status & ENRICHMENT_EMBEDDING:
                logger.debug(
                    "embed_episode.skipped_already_done",
                    episode_id=episode_id,
                    enrichment_status=episode.enrichment_status,
                )
                return

            # Archived-project guard: pause-and-resume. Fail-closed (missing
            # row counts as archived). Early return sets no bits, so
            # un-archiving resumes via reconcile or retry.
            if await ProjectRepository(idempotency_db).is_archived(
                uuid.UUID(org_id), uuid.UUID(project_id)
            ):
                logger.info(
                    "embed_episode.project_archived_skipping",
                    episode_id=episode_id,
                    org_id=org_id,
                    project_id=project_id,
                )
                return
        finally:
            # Reset before the session closes — the context is session-scoped
            # and would otherwise stay on the pooled connection.
            await release_tenant_context(idempotency_db)

    # ── 3. Generate embedding ────────────────────────────────────────────
    try:
        embedding = (await embed_passage([episode_text]))[0]
    except Exception as e:
        logger.error(
            "embed_episode.embedding_failed",
            episode_id=episode_id,
            error=str(e),
        )
        raise

    # ── 4. Store in pgvector and update enrichment_status ─────────────────
    # The pgvector asyncpg codec IS registered via ``init_db_engine``, so
    # the vector goes in as native ``list[float]`` — the codec encodes it
    # and the static ``::vector(768)`` cast only asserts the dimension.
    # Passing a ``str`` literal here breaks decoding (asyncpg DataError).
    # ``embed_passage`` already validated the width — the cast cannot
    # silently reshape.

    try:
        async with session_factory() as db:
            try:
                await rls.set_tenant_context(db, org_id)
                await db.execute(
                    text(
                        "UPDATE episodes SET embedding = "  # noqa: S608
                        f"CAST(:embedding AS vector({CANONICAL_EMBED_DIM})) "
                        "WHERE id = :id"
                        # S608 justification: interpolates the int constant
                        # CANONICAL_EMBED_DIM into a static CAST, never user input.
                    ),
                    {"embedding": embedding, "id": episode_id},
                )
                # Set bit 1 on enrichment_status to mark completion.
                episode_repo = EpisodeRepository(db)
                await episode_repo.apply_enrichment_bits(
                    uuid.UUID(episode_id), ENRICHMENT_EMBEDDING
                )
                await db.commit()
            finally:
                await release_tenant_context(db)

        logger.info(
            "embed_episode.completed",
            episode_id=episode_id,
            dim=len(embedding),
        )
    finally:
        if _own_engine:
            await engine.dispose()
