"""Context assembly service — orchestrates retrieval, formatting, and caching.

Flow:
    1. Check Redis cache → return cached if exists
    2. Run hybrid search (vector + BM25 + RRF)
    3. Format as text or JSON
    4. Cache result
    5. Return response with metadata

This service is the primary entry point for the context assembly endpoint.
It delegates retrieval to ``HybridRetriever``, caching to ``CacheService``,
and formatting to ``context_formatter``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import UUID

import orjson
import structlog

from middleware.metrics import context_latency_seconds
from packages.reranker import RerankerFactory
from services.cache_service import CacheService
from services.context_formatter import format_json, format_text
from services.hybrid_retriever import HybridRetriever

if TYPE_CHECKING:
    from schemas.organization_config import OrgConfigBase

logger = structlog.get_logger()


class ContextService:
    """Assembles context blocks for LLM injection.

    Orchestrates the retrieval → format → cache pipeline.  Every public
    method is idempotent — the same inputs produce the same output (with
    cache reflecting staleness).

    Args:
        db: An async SQLAlchemy session (request-scoped).
        org_id: The authenticated organization UUID.
        redis: An optional async Redis client for caching.  When ``None``,
            caching is disabled but the service continues to function.
    """

    def __init__(
        self,
        db: object,
        org_id: UUID,
        redis: object | None = None,
        graph_backends: list | None = None,
        org_config: OrgConfigBase | None = None,
    ) -> None:
        reranker = RerankerFactory.create(org_config) if org_config else None
        self._retriever = HybridRetriever(
            db, org_id, redis, graph_backends=graph_backends, org_config=org_config,
            reranker=reranker,
        )
        self._cache = (
            CacheService(redis, default_ttl=org_config.context_cache_ttl if org_config else None)
            if redis
            else None
        )
        self._org_id = org_id

    # ── Public API ──────────────────────────────────────────────────────────────

    async def assemble(
        self,
        project_id: UUID,
        query: str,
        limit: int = 20,
        format: str = "text",  # noqa: A002
    ) -> dict:
        """Assemble a context block for a project from a natural-language query.

        Full pipeline:
        1. Build a cache key from (org_id, project_id, query) and check Redis.
        2. On cache miss, run hybrid search across episodes, facts,
           entities, and communities.
        3. Format results as plain text or structured JSON.
        4. Store the formatted result in Redis with a configurable TTL.
        5. Return the context string along with assembly metadata
           (cache hit, timing, source counts).

        Args:
            project_id: The UUID of the project to retrieve context for.
            query: A natural-language query describing the context needed.
            limit: Maximum items per source type (1–100).
            format: Output format — ``"text"`` (default) or ``"json"``.

        Returns:
            A dict with:
            - ``context``: The assembled context string.
            - ``metadata``: Dict with ``cache_hit``, ``assembly_time_ms``,
              ``source_counts``, and ``total_items``.
        """
        start = time.monotonic()

        # ═══════════════════════════════════════════════════════════════════
        # Step 1 — Check cache
        # ═══════════════════════════════════════════════════════════════════
        cache_key: str | None = None
        if self._cache is not None:
            cache_key = self._cache.build_context_cache_key(
                str(self._org_id),
                str(project_id),
                query,
            )
            cached = await self._cache.get(cache_key)
            if cached is not None:
                elapsed = (time.monotonic() - start) * 1000
                context_latency_seconds.labels(type="warm").observe(elapsed / 1000)
                logger.debug(
                    "context.cache_hit",
                    org_id=str(self._org_id),
                    project_id=str(project_id),
                    query_hash=query,
                    assembly_time_ms=round(elapsed, 1),
                )
                return {
                    "context": cached,
                    "metadata": {
                        "cache_hit": True,
                        "assembly_time_ms": round(elapsed, 1),
                        "source_counts": {},
                        "total_items": 0,
                    },
                }

        # ═══════════════════════════════════════════════════════════════════
        # Step 2 — Run hybrid search
        # ═══════════════════════════════════════════════════════════════════
        results = await self._retriever.hybrid_search(query, project_id, limit)

        # ═══════════════════════════════════════════════════════════════════
        # Step 3 — Format
        # ═══════════════════════════════════════════════════════════════════
        if format == "json":
            context_data = format_json(
                results.get("episodes", []),
                results.get("facts", []),
                results.get("entities", []),
                results.get("communities", []),
            )
            context_str: str = orjson.dumps(context_data).decode()
        else:
            context_str = format_text(
                results.get("episodes", []),
                results.get("facts", []),
                results.get("entities", []),
                results.get("communities", []),
            )

        # ═══════════════════════════════════════════════════════════════════
        # Step 4 — Cache result
        # ═══════════════════════════════════════════════════════════════════
        if self._cache is not None and cache_key is not None:
            await self._cache.set(cache_key, context_str, ttl=30)

        elapsed = (time.monotonic() - start) * 1000
        context_latency_seconds.labels(type="cold").observe(elapsed / 1000)
        logger.debug(
            "context.assembled",
            org_id=str(self._org_id),
            project_id=str(project_id),
            query_hash=query,
            format=format,
            assembly_time_ms=round(elapsed, 1),
            total_items=results.get("total_items", 0),
        )

        return {
            "context": context_str,
            "metadata": {
                "cache_hit": False,
                "assembly_time_ms": round(elapsed, 1),
                "source_counts": results["source_counts"],
                "total_items": results.get("total_items", 0),
            },
        }
