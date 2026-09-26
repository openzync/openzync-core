"""Embedding worker for facts — generates pgvector embeddings for extracted facts.

Runs after facts are extracted from episodes.  Generates embeddings via
the configured BYOK LLM backend and stores them in ``facts.embedding``.

Queue: high-priority (real-time ingestion).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from core.exceptions import SearchLegFailedError
from workers.tasks.base import with_retry

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger()


def _is_retryable(exc: Exception) -> bool:
    """Return True when an embedding error is worth retrying.

    4xx client errors (detected via the ``status_code`` attribute, which
    covers ``openai.BadRequestError`` and its siblings without importing
    the SDK) are permanent — retrying cannot succeed — so they return
    False, except 408 (timeout) and 429 (rate-limit) which are transient.
    Everything else (5xx, timeouts, network errors, no status) is retried.
    """
    status_code = getattr(exc, "status_code", None)
    return status_code in (408, 429) or not (
        isinstance(status_code, int) and 400 <= status_code <= 499
    )


async def _retire_fact(
    session_factory: Callable[..., Any],
    engine: Any,
    own_engine: bool,
    fact_id: str,
) -> None:
    """Mark a fact as permanently unembeddable without storing a vector.

    Sets ``embedded_at`` with ``embedding`` left NULL so
    ``reconcile_enrichment`` stops re-enqueueing the fact. Disposes the
    engine when this worker created it.

    Args:
        session_factory: Async session factory bound to the worker's engine.
        engine: The worker's async engine (disposed when ``own_engine``).
        own_engine: True when this worker created the engine itself.
        fact_id: UUID of the fact to retire.
    """
    # Any keeps sqlalchemy out of module top-level (ARQ lazy-import convention).
    from sqlalchemy import text

    try:
        async with session_factory() as db:
            await db.execute(
                text("UPDATE facts SET embedded_at = now() WHERE id = :id"),
                {"id": fact_id},
            )
            await db.commit()
    finally:
        if own_engine:
            await engine.dispose()


@with_retry(max_retries=3, base_delay_s=2.0, is_retryable=_is_retryable)
async def embed_fact(
    ctx: object,
    fact_id: str,
    content: str | None = None,
    trace_id: str = "",
    **kwargs: object,  # noqa: ARG002 — accepts org_id, user_id from API caller
) -> None:
    """Generate an embedding for a fact and store it in ``facts.embedding``.

    The embedding backend comes from the per-org config
    (``org_cfg.embedding_backend``); the model is the frozen canonical
    model (``core.embeddings.resolve_embed_model``). There is no env-var
    fallback — if no backend is configured the task raises. Any vector
    that is not exactly ``CANONICAL_EMBED_DIM`` raises
    ``ExternalServiceError`` and is never stored (no retire — a dim
    mismatch under the freeze means the provider serves the wrong model
    and must stay loud until fixed).

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        fact_id: UUID of the fact to embed.
        content: Fact text content to embed. If not provided (e.g. when
            called from ``fact_service``), it will be fetched from the DB.
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.
        **kwargs: Additional context (org_id, user_id) forwarded from the caller.

    Raises:
        SearchLegFailedError: If the org config cannot be fetched or no
            embedding backend is configured (same taxonomy as
            ``embed_episode`` — ARQ retries).
        ExternalServiceError: If the provider returns a non-canonical-dim
            vector.
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    # ── Lazy imports (ARQ workers run in a separate process) ──────────────
    from sqlalchemy import text

    from core.config import settings
    from core.db import get_async_session
    from core.embeddings import (
        CANONICAL_EMBED_DIM,
        resolve_embed_model,
        validate_embedding_dim,
    )
    from core.llm import resolve_backend

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

            bao_client = ctx.get("openbao_client") if isinstance(ctx, dict) else None
            if bao_client is not None:
                org_cfg = await get_org_config(
                    uuid.UUID(_org_id), redis=None, bao_client=bao_client
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
                        uuid.UUID(_org_id), redis=None, bao_client=_tmp_bao
                    )
        except Exception as exc:
            logger.warning(
                "embed_fact.org_config_fetch_failed",
                org_id=_org_id,
                exc_info=True,
            )
            raise SearchLegFailedError(
                leg_name="embedding",
                message=f"Failed to fetch org config for org {_org_id}: {exc}",
                original_error=str(exc),
            ) from exc

    if org_cfg is None:
        raise SearchLegFailedError(
            leg_name="embedding",
            message=f"Org config not found for org {_org_id}",
        )
    if org_cfg.embedding_backend is None:
        raise SearchLegFailedError(
            leg_name="embedding",
            message=f"No embedding backend configured for org {_org_id}",
            original_error=f"org_cfg.embedding_backend is None for org {_org_id}",
        )

    _embedding_backend = org_cfg.embedding_backend
    _embedding_model = resolve_embed_model(org_cfg.embedding_backend)
    _org_config_dict = org_cfg.to_llm_config_dict()

    # ── 1. Resolve the embedding backend ──────────────────────────────────
    llm = await resolve_backend(
        provider=_embedding_backend,
        org_config=_org_config_dict,
        mode="embedding",
    )

    # ── 2. Generate embedding ────────────────────────────────────────────
    try:
        result = await llm.embed([content], model=_embedding_model)
        embedding = result.embeddings[0]
    except Exception as e:
        if not _is_retryable(e):
            # Permanent 4xx (bad request, unknown model, rejected params):
            # retrying cannot succeed, so retire the fact and raise.
            logger.error(
                "embed_fact.embedding_non_retryable",
                fact_id=fact_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            await _retire_fact(session_factory, engine, _own_engine, fact_id)
        else:
            logger.error(
                "embed_fact.embedding_failed",
                fact_id=fact_id,
                error=str(e),
            )
        raise

    # ── 3. Validate canonical dimension — fail loud, never store ─────────
    # Deliberately no retire: under the freeze a dim mismatch means the
    # provider serves the wrong model (operator fix required). The fact
    # stays NULL/NULL so reconcile keeps it visible via re-enqueue.
    validate_embedding_dim(embedding, source="embed_fact")

    # ── 4. Store in pgvector ──────────────────────────────────────────────
    # The pgvector asyncpg codec IS registered via ``init_db_engine``, so
    # the vector goes in as native ``list[float]`` — the codec encodes it
    # and the static ``::vector(768)`` cast only asserts the dimension.
    # Passing a ``str`` literal here breaks decoding (asyncpg DataError).
    # The dimension was validated above — the cast cannot silently reshape.
    try:
        async with session_factory() as db:
            await db.execute(
                text(
                    "UPDATE facts SET embedding = "  # noqa: S608
                    f"CAST(:embedding AS vector({CANONICAL_EMBED_DIM})), "
                    "embedded_at = now() WHERE id = :id"
                    # S608 justification: interpolates the int constant
                    # CANONICAL_EMBED_DIM into a static CAST, never user input.
                ),
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
