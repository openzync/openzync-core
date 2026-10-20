"""Embedding worker for facts — generates pgvector embeddings for extracted facts.

Runs after facts are extracted from episodes.  Generates embeddings with
the single local embedder (``core.embeddings.embed_passage``) and stores
them in ``facts.embedding``.

Queue: high-priority (real-time ingestion).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from core.rls import release_tenant_context
from workers.tasks.base import with_retry, with_task_metrics

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
    org_id: str,
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
        org_id: UUID of the owning organization (RLS tenant scope).
    """
    # Any keeps sqlalchemy out of module top-level (ARQ lazy-import convention).
    from sqlalchemy import text

    from core import rls

    try:
        async with session_factory() as db:
            try:
                await rls.set_tenant_context(db, org_id)
                await db.execute(
                    text("UPDATE facts SET embedded_at = now() WHERE id = :id"),
                    {"id": fact_id},
                )
                await db.commit()
            finally:
                await release_tenant_context(db)
    finally:
        if own_engine:
            await engine.dispose()


@with_task_metrics
@with_retry(max_retries=3, base_delay_s=2.0, is_retryable=_is_retryable)
async def embed_fact(
    ctx: object,
    fact_id: str,
    content: str | None = None,
    trace_id: str = "",
    **kwargs: Any,  # noqa: ARG002 — accepts org_id, user_id from API caller
) -> None:
    """Generate an embedding for a fact and store it in ``facts.embedding``.

    Embeds with the single local embedder (``core.embeddings.embed_passage``)
    — the document prefix, since a fact is written to the corpus side of
    the vector store.  Any vector that is not exactly ``CANONICAL_EMBED_DIM``
    raises ``ExternalServiceError`` and is never stored (no retire — see
    the note at the validation step below).

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        fact_id: UUID of the fact to embed.
        content: Fact text content to embed. If not provided (e.g. when
            called from ``fact_service``), it will be fetched from the DB.
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.
        **kwargs: Additional context (org_id, user_id) forwarded from the caller.

    Raises:
        ExternalServiceError: If the embedder returns a non-canonical-dim
            vector.
        RuntimeError: If ``org_id`` is missing — the RLS tenant context
            cannot be derived, and an unbound session would silently see
            zero rows.
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    # The RLS tenant scope.  Without it the session sees no facts at all
    # under enforced row-level security, so a missing org_id is a loud
    # wiring error — never a silently empty result.
    org_id = kwargs.get("org_id")
    if not org_id:
        raise RuntimeError(
            "embed_fact requires an org_id — the RLS tenant context cannot "
            "be derived without it."
        )

    # ── Lazy imports (ARQ workers run in a separate process) ──────────────
    from sqlalchemy import text

    from core import rls
    from core.config import settings
    from core.db import get_async_session
    from core.embeddings import CANONICAL_EMBED_DIM, embed_passage

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
            try:
                await rls.set_tenant_context(db, org_id)
                result = await db.execute(
                    text("SELECT content FROM facts WHERE id = :id"),
                    {"id": fact_id},
                )
                row = result.one_or_none()
                if row is None:
                    logger.error("embed_fact.fact_not_found", fact_id=fact_id)
                    return
                content = row[0]
            finally:
                await release_tenant_context(db)

    # ── 1. Generate embedding ────────────────────────────────────────────
    try:
        embedding = (await embed_passage([content]))[0]
    except Exception as e:
        # Deliberately no retire on a dimension mismatch: ``embed_passage``
        # rejects it before any vector is produced, which means the local
        # model itself is serving the wrong width (operator fix required,
        # retrying cannot succeed). The fact stays NULL/NULL so
        # ``reconcile_enrichment`` keeps it visible via re-enqueue.
        # ``_is_retryable`` returns True for that ``ExternalServiceError``
        # (no 4xx ``status_code``), so it lands in the else-branch below.
        if not _is_retryable(e):
            # Permanent 4xx (bad request, unknown model, rejected params):
            # retrying cannot succeed, so retire the fact and raise.
            logger.error(
                "embed_fact.embedding_non_retryable",
                fact_id=fact_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            await _retire_fact(session_factory, engine, _own_engine, fact_id, org_id)
        else:
            logger.error(
                "embed_fact.embedding_failed",
                fact_id=fact_id,
                error=str(e),
            )
        raise

    # ── 2. Store in pgvector ──────────────────────────────────────────────
    # ``embed_passage`` already validated the canonical width and refused
    # anything else, so reaching here means the vector is storable.
    # The pgvector asyncpg codec IS registered via ``init_db_engine``, so
    # the vector goes in as native ``list[float]`` — the codec encodes it
    # and the static ``::vector(768)`` cast only asserts the dimension.
    # Passing a ``str`` literal here breaks decoding (asyncpg DataError).
    try:
        async with session_factory() as db:
            try:
                await rls.set_tenant_context(db, org_id)
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
            finally:
                await release_tenant_context(db)

        logger.info(
            "embed_fact.completed",
            fact_id=fact_id,
            dim=len(embedding),
        )
    finally:
        if _own_engine:
            await engine.dispose()
