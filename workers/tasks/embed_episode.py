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
    trace_id: str = "",
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
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.

    Raises:
        ValueError: If the embedding dimension does not match
            ``EMBEDDING_DIM``.
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    # ── Lazy imports (ARQ workers run in a separate process) ──────────────
    from core.config import settings
    from core.db import get_async_session
    from core.llm import resolve_backend
    from sqlalchemy import text

    logger.info("embed_episode.started", episode_id=episode_id, trace_id=trace_id)

    # ── 1. Resolve the embedding backend ──────────────────────────────────
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
    # Use the shared engine from worker context.  Under
    # `services/worker/worker.py` this is injected automatically.
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
        if _own_engine:
            await engine.dispose()
