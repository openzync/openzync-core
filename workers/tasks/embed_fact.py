"""Embedding worker for facts — generates pgvector embeddings for extracted facts.

Runs after facts are extracted from episodes.  Generates embeddings via
the configured BYOK LLM backend and stores them in ``facts.embedding``.

Queue: high-priority (real-time ingestion).
"""

from __future__ import annotations

import structlog

from workers.tasks.base import with_retry

logger = structlog.get_logger()


@with_retry(max_retries=3, base_delay_s=2.0)
async def embed_fact(ctx: object, fact_id: str, content: str) -> None:
    """Generate an embedding for a fact and store it in ``facts.embedding``.

    Resolution chain for the embedding model:
        1. ``EMBEDDING_BACKEND`` env var (if set) → overrides the chat LLM.
        2. ``LLM_BACKEND`` env var → fallback.
        3. Auto-detect (Ollama on localhost) → last resort.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        fact_id: UUID of the fact to embed.
        content: Fact text content to embed.

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

    logger.info("embed_fact.started", fact_id=fact_id)

    # ── 1. Resolve the embedding backend ──────────────────────────────────
    provider = settings.EMBEDDING_BACKEND or None
    llm = await resolve_backend(provider=provider)

    # ── 2. Generate embedding ────────────────────────────────────────────
    try:
        result = await llm.embed([content], model=settings.EMBEDDING_MODEL)
        embedding = result.embeddings[0]
    except Exception as e:
        logger.error(
            "embed_fact.embedding_failed",
            fact_id=fact_id,
            error=str(e),
        )
        raise

    # ── 3. Validate dimension matches config ──────────────────────────────
    if len(embedding) != settings.EMBEDDING_DIM:
        logger.error(
            "embed_fact.dimension_mismatch",
            fact_id=fact_id,
            got=len(embedding),
            expected=settings.EMBEDDING_DIM,
        )
        raise ValueError(
            f"Embedding dimension mismatch: got {len(embedding)}, "
            f"expected {settings.EMBEDDING_DIM}"
        )

    # ── 4. Store in pgvector ──────────────────────────────────────────────
    engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=2,
    )
    session_factory = get_async_session(engine)

    try:
        async with session_factory() as db:
            await db.execute(
                text("UPDATE facts SET embedding = :embedding WHERE id = :id"),
                {"embedding": embedding, "id": fact_id},
            )
            await db.commit()

        logger.info(
            "embed_fact.completed",
            fact_id=fact_id,
            dim=len(embedding),
        )
    finally:
        await engine.dispose()
