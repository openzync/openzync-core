"""Embedding worker — generates pgvector embeddings for episode content.

Runs after entity extraction (enrichment_status bit 0 must be set).
Generates embeddings via the configured BYOK LLM backend and stores
them in the ``episodes.embedding`` column.

Queue: high-priority (real-time ingestion).
"""

from __future__ import annotations

import structlog

from core.exceptions import EpisodeNotFoundError, SearchLegFailedError
from workers.tasks.base import ENRICHMENT_EMBEDDING, with_retry

logger = structlog.get_logger()


@with_retry(max_retries=3, base_delay_s=2.0)
async def embed_episode(
    ctx: object,
    episode_id: str,
    org_id: str,
    project_id: str,
    content: str,
    trace_id: str = "",
    metadata: dict | None = None,
) -> None:
    """Generate an embedding for an episode and store it in pgvector.

    The embedding backend comes from the per-org config
    (``org_cfg.embedding_backend``); the model is the frozen canonical
    model (``core.embeddings.resolve_embed_model``). There is no env-var
    fallback — if no backend is configured the task raises
    ``SearchLegFailedError`` so ARQ retries. Any vector that is not
    exactly ``CANONICAL_EMBED_DIM`` raises ``ExternalServiceError`` and
    is never stored.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        episode_id: UUID of the episode to embed.
        org_id: UUID of the owning organisation (for observability / RLS).
        project_id: UUID of the project for project scoping (observability).
        content: Episode message text to embed.
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.
        metadata: Optional metadata dict forwarded from the enrichment pipeline.

    Raises:
        EpisodeNotFoundError: If no episode exists for ``episode_id``.
        SearchLegFailedError: If org config fetch fails, org is not found,
            or no embedding backend is configured.
        ExternalServiceError: If the provider returns a non-canonical-dim
            vector.
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    # ── Lazy imports (ARQ workers run in a separate process) ──────────────
    import uuid

    from sqlalchemy import text

    from core.config import settings
    from core.db import get_async_session
    from core.embeddings import (
        CANONICAL_EMBED_DIM,
        format_vector_literal,
        resolve_embed_model,
        validate_embedding_dim,
    )
    from core.llm import resolve_backend
    from core.org_config import get_org_config
    from repositories.episode_repository import EpisodeRepository

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
        if episode.enrichment_status & ENRICHMENT_EMBEDDING:
            logger.debug(
                "embed_episode.skipped_already_done",
                episode_id=episode_id,
                enrichment_status=episode.enrichment_status,
            )
            return

    # ── 2b. Fetch per-organization config ──────────────────────────────────
    org_cfg = None
    try:
        bao_client = ctx.get("openbao_client") if isinstance(ctx, dict) else None
        if bao_client is not None:
            org_cfg = await get_org_config(
                uuid.UUID(org_id), redis=None, bao_client=bao_client
            )
        else:
            from core.config import BootstrapSettings
            from core.openbao import OpenBaoClient

            bootstrap = BootstrapSettings()
            async with OpenBaoClient(
                bootstrap.OPENBAO_ADDR,
                bootstrap.OPENBAO_ROLE_ID,
                bootstrap.OPENBAO_SECRET_ID,
                timeout=10.0,
            ) as _tmp_bao:
                org_cfg = await get_org_config(
                    uuid.UUID(org_id), redis=None, bao_client=_tmp_bao
                )
    except Exception as exc:
        logger.warning(
            "embed_episode.org_config_fetch_failed",
            org_id=org_id,
            exc_info=True,
        )
        raise SearchLegFailedError(
            leg_name="embedding",
            message=f"Failed to fetch org config for org {org_id}: {exc}",
            original_error=str(exc),
        ) from exc

    if org_cfg is None:
        raise SearchLegFailedError(
            leg_name="embedding",
            message=f"Org config not found for org {org_id}",
        )

    if org_cfg.embedding_backend is None:
        raise SearchLegFailedError(
            leg_name="embedding",
            message=f"No embedding backend configured for org {org_id}",
            original_error=f"org_cfg.embedding_backend is None for org {org_id}",
        )

    _embedding_backend = org_cfg.embedding_backend
    _embedding_model = resolve_embed_model(org_cfg.embedding_backend)
    _org_config_dict = org_cfg.to_llm_config_dict()

    # ── 3. Resolve the embedding backend ──────────────────────────────────
    llm = await resolve_backend(
        provider=_embedding_backend,
        org_config=_org_config_dict,
        mode="embedding",
    )

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

    # ── 5. Validate canonical dimension — fail loud, never store ─────────
    validate_embedding_dim(embedding, source="embed_episode")

    # ── 4. Store in pgvector and update enrichment_status ─────────────────
    # No pgvector asyncpg codec is registered, so the vector goes in as an
    # explicit ``[...]`` literal with a static ``::vector(768)`` cast. The
    # dimension was validated above — the cast cannot silently reshape.

    try:
        async with session_factory() as db:
            await db.execute(
                text(
                    "UPDATE episodes SET embedding = "  # noqa: S608
                    f"CAST(:embedding AS vector({CANONICAL_EMBED_DIM})) "
                    "WHERE id = :id"
                    # S608 justification: interpolates the int constant
                    # CANONICAL_EMBED_DIM into a static CAST, never user input.
                ),
                {"embedding": format_vector_literal(embedding), "id": episode_id},
            )
            # Set bit 1 on enrichment_status to mark completion.
            episode_repo = EpisodeRepository(db)
            await episode_repo.apply_enrichment_bits(
                uuid.UUID(episode_id), ENRICHMENT_EMBEDDING
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
