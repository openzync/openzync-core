"""Embedding worker — generates pgvector embeddings for episode content.

Runs after entity extraction (enrichment_status bit 0 must be set).
Generates embeddings via the configured BYOK LLM backend and stores
them in the ``episodes.embedding`` column.

Queue: high-priority (real-time ingestion).
"""

from __future__ import annotations

import structlog

from workers.tasks.base import ENRICHMENT_EMBEDDING, with_retry

logger = structlog.get_logger()


@with_retry(max_retries=3, base_delay_s=2.0)
async def embed_episode(
    ctx: object,
    episode_id: str,
    org_id: str,
    content: str,
) -> None:
    """Generate an embedding for an episode and store it in pgvector.

    Resolution chain for the embedding model:
        1. ``EMBEDDING_BACKEND`` env var (if set) → overrides the chat LLM
           provider.
        2. ``LLM_BACKEND`` env var → fallback when ``EMBEDDING_BACKEND`` is
           empty.
        3. Auto-detect (Ollama on localhost) → last resort.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        episode_id: UUID of the episode to embed.
        org_id: UUID of the owning organisation (for observability / RLS).
        content: Episode message text to embed.

    Raises:
        ValueError: If the embedding dimension does not match
            ``EMBEDDING_DIM``.
    """
    # ── Lazy imports (ARQ workers run in a separate process) ──────────────
    from core.config import settings
    from core.db import get_async_session
    from core.llm import resolve_backend
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    logger.info("embed_episode.started", episode_id=episode_id)

    # ── 1. Resolve the embedding backend ──────────────────────────────────
    # note: EMBEDDING_BACKEND is a separate config from LLM_BACKEND
    # because many deployments use a dedicated embedding service (e.g.
    # nomic-embed-text on Ollama) alongside a chat LLM (e.g. GPT-4o). When
    # EMBEDDING_BACKEND is empty we fall back to the chat LLM provider.
    provider = settings.EMBEDDING_BACKEND or None
    llm = await resolve_backend(provider=provider)

    # ── 2. Generate embedding ────────────────────────────────────────────
    try:
        result = await llm.embed([content], model=settings.EMBEDDING_MODEL)
        embedding = result.embeddings[0]
    except Exception as e:
        logger.error(
            "embed_episode.embedding_failed",
            episode_id=episode_id,
            error=str(e),
        )
        raise

    # ── 3. Validate dimension matches config ──────────────────────────────
    if len(embedding) != settings.EMBEDDING_DIM:
        logger.error(
            "embed_episode.dimension_mismatch",
            episode_id=episode_id,
            got=len(embedding),
            expected=settings.EMBEDDING_DIM,
        )
        raise ValueError(
            f"Embedding dimension mismatch: got {len(embedding)}, "
            f"expected {settings.EMBEDDING_DIM}"
        )

    # ── 4. Store in pgvector and update enrichment_status ─────────────────
    # note: We create a short-lived engine here because ARQ workers
    # run in a separate process and may not share the app's engine. For
    # higher throughput, consider passing the engine from the worker
    # initialisation context instead.
    engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=2,
    )
    session_factory = get_async_session(engine)

    try:
        async with session_factory() as db:
            # ⚠️ VECTOR SERIALIZATION: When the column is ``vector(1536)``,
            # asyncpg handles list[float] → vector conversion automatically.
            # If the column is still ``Text`` (pre-migration), this stores
            # the Python repr. The Alembic migration for pgvector should be
            # run before this worker is deployed to production.
            await db.execute(
                text("UPDATE episodes SET embedding = :embedding WHERE id = :id"),
                {"embedding": embedding, "id": episode_id},
            )
            # Set bit 1 on enrichment_status to mark completion.
            await db.execute(
                text(
                    "UPDATE episodes "
                    "SET enrichment_status = enrichment_status | :bit "
                    "WHERE id = :id"
                ),
                {"bit": ENRICHMENT_EMBEDDING, "id": episode_id},
            )
            await db.commit()

        logger.info(
            "embed_episode.completed",
            episode_id=episode_id,
            dim=len(embedding),
        )
    finally:
        await engine.dispose()
