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
async def embed_fact(
    ctx: object,
    fact_id: str,
    content: str | None = None,
    trace_id: str = "",
    **kwargs: object,  # noqa: ARG002 — accepts org_id, user_id from API caller
) -> None:
    """Generate an embedding for a fact and store it in ``facts.embedding``.

    Resolution chain for the embedding model:
        1. ``EMBEDDING_BACKEND`` env var (if set) → overrides the chat LLM.
        2. ``LLM_BACKEND`` env var → fallback.
        3. Auto-detect (Ollama on localhost) → last resort.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        fact_id: UUID of the fact to embed.
        content: Fact text content to embed. If not provided (e.g. when
            called from ``fact_service``), it will be fetched from the DB.
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

    logger.info("embed_fact.started", fact_id=fact_id, trace_id=trace_id)

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

    # ── 0. Fetch content from DB if not provided ──────────────────────────
    if content is None:
        async with session_factory() as db:
            result = await db.execute(
                text("SELECT content FROM facts WHERE id = :id"),
                {"id": fact_id},
            )
            row = result.one_or_none()
            if row is None:
                logger.error("embed_fact.fact_not_found", fact_id=fact_id)
                return
            content = row[0]

    # ── 0b. Fetch per-organization config if org_id is available ─────────
    _org_id = kwargs.get("org_id")
    import uuid

    org_cfg = None
    if _org_id:
        try:
            from core.org_config import get_org_config

            async with session_factory() as _cfg_db:
                org_cfg = await get_org_config(uuid.UUID(_org_id), _cfg_db, redis=None)
        except Exception:
            logger.warning(
                "embed_fact.org_config_fetch_failed",
                org_id=_org_id,
                exc_info=True,
            )

    # No env-var fallback — skip if org config is unavailable or
    # no embedding backend is configured.
    if org_cfg is None or org_cfg.embedding_backend is None:
        logger.warning(
            "embed_fact.skipped_no_embedding_config",
            org_id=_org_id,
        )
        return

    _embedding_backend = org_cfg.embedding_backend
    _embedding_model = org_cfg.embedding_model
    _embedding_dim = org_cfg.embedding_dim
    _org_config_dict = org_cfg.to_llm_config_dict()

    # ── 1. Resolve the embedding backend ──────────────────────────────────
    llm = await resolve_backend(provider=_embedding_backend, org_config=_org_config_dict)

    # ── 2. Generate embedding ────────────────────────────────────────────
    try:
        result = await llm.embed([content], model=_embedding_model)
        embedding = result.embeddings[0]
    except Exception as e:
        logger.error(
            "embed_fact.embedding_failed",
            fact_id=fact_id,
            error=str(e),
        )
        raise

    # ── 3. Validate dimension matches config ──────────────────────────────
    if len(embedding) != _embedding_dim:
        logger.error(
            "embed_fact.dimension_mismatch",
            fact_id=fact_id,
            got=len(embedding),
            expected=_embedding_dim,
        )
        raise ValueError(
            f"Embedding dimension mismatch: got {len(embedding)}, "
            f"expected {_embedding_dim}"
        )

    # ── 4. Store in pgvector ──────────────────────────────────────────────
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
        if _own_engine:
            await engine.dispose()
