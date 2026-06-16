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
    metadata: dict | None = None,
) -> None:
    """Generate an embedding for an episode and store it in pgvector.

    The embedding backend, model, and dimension come exclusively from the
    per-org config (``org_cfg.embedding_backend`` / ``embedding_model`` /
    ``embedding_dim``) resolved from the ``organizations.config`` JSONB
    column.  There is no env-var fallback — if any required field is
    ``None`` the task logs a warning and returns early.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        episode_id: UUID of the episode to embed.
        org_id: UUID of the owning organisation (for observability / RLS).
        content: Episode message text to embed.
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.

    Raises:
        ValueError: If the embedding dimension does not match
            the per-org config ``embedding_dim``.
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    # ── Lazy imports (ARQ workers run in a separate process) ──────────────
    from core.config import settings
    from core.db import get_async_session
    from core.llm import resolve_backend
    from sqlalchemy import text

    logger.info("embed_episode.started", episode_id=episode_id, trace_id=trace_id)

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

    # ── 2. Fetch per-organization config ───────────────────────────────────
    import uuid
    from core.org_config import get_org_config

    org_cfg = None
    try:
        async with session_factory() as _db:
            org_cfg = await get_org_config(uuid.UUID(org_id), _db, redis=None)
    except Exception:
        logger.warning(
            "embed_episode.org_config_fetch_failed",
            org_id=org_id,
            exc_info=True,
        )

    # No env-var fallback — skip if org config is unavailable or
    # no embedding backend is configured.
    if org_cfg is None or org_cfg.embedding_backend is None:
        logger.warning(
            "embed_episode.skipped_no_embedding_config",
            org_id=org_id,
        )
        return

    _embedding_backend = org_cfg.embedding_backend
    _embedding_model = org_cfg.embedding_model
    _embedding_dim = org_cfg.embedding_dim
    _org_config_dict = org_cfg.to_llm_config_dict()

    # ── 3. Resolve the embedding backend ──────────────────────────────────
    llm = await resolve_backend(provider=_embedding_backend, org_config=_org_config_dict)

    # ── 4. Generate embedding ────────────────────────────────────────────
    try:
        result = await llm.embed([content], model=_embedding_model)
        embedding = result.embeddings[0]
    except Exception as e:
        logger.error(
            "embed_episode.embedding_failed",
            episode_id=episode_id,
            error=str(e),
        )
        raise

    # ── 5. Validate dimension matches config ──────────────────────────────
    if len(embedding) != _embedding_dim:
        logger.error(
            "embed_episode.dimension_mismatch",
            episode_id=episode_id,
            got=len(embedding),
            expected=_embedding_dim,
        )
        raise ValueError(
            f"Embedding dimension mismatch: got {len(embedding)}, "
            f"expected {_embedding_dim}"
        )

    # ── 4. Store in pgvector and update enrichment_status ─────────────────

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
