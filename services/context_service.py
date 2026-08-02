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

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
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

from repositories.episode_blob_repository import EpisodeBlobRepository
from services.blob_storage_service import BlobStorageService

logger = structlog.get_logger()


def _preview(items: list[dict[str, Any]], max_chars: int = 500) -> str | None:
    """Build a compact preview string for the top result in a list.

    Includes all available scores (``score``, ``rrf_score``, ``reranker_score``)
    and a truncated content preview.  Returns ``None`` when *items* is empty.

    Args:
        items: Ranked result list (episodes or facts) from the RRF merge.
        max_chars: Maximum characters of content to include.

    Returns:
        A preview string like ``"[rrf_score=0.0161] Hey, I'm thinking of..."``
        or ``None`` if there are no items to preview.
    """
    if not items:
        return None
    first = items[0]
    content = (first.get("content") or "")[:max_chars]
    scores = " | ".join(
        f"{k}={v:.4f}"
        for k in ("score", "rrf_score", "reranker_score")
        if (v := first.get(k)) is not None
    )
    suffix = "..." if len(first.get("content") or "" ) > max_chars else ""
    if scores:
        return f"[{scores}] {content}{suffix}"
    return f"{content}{suffix}"


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
        self._db = db
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
        self._org_config = org_config

    # ── Public API ──────────────────────────────────────────────────────────────

    async def assemble(
        self,
        project_id: UUID,
        query: str,
        limit: int = 20,
        format: str = "text",  # noqa: A002
        as_of: datetime | None = None,
    ) -> dict:
        """Assemble a context block for a project from a natural-language query.

        Full pipeline:
        1. Build a cache key from (org_id, project_id, query, as_of) and
           check Redis — different effective-at timestamps get distinct
           keys so a cached as-of result never poisons another timestamp's
           30s cache window.
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
            as_of: Effective-at timestamp (UTC) for fact retrieval.  Facts
                superseded before this instant are excluded.  ``None``
                means "now".

        Returns:
            A dict with:
            - ``context``: The assembled context string.
            - ``metadata``: Dict with ``cache_hit``, ``assembly_time_ms``,
              ``source_counts``, ``total_items``, and ``as_of``.
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
                as_of=as_of.isoformat() if as_of is not None else None,
            )
            cached = await self._cache.get(cache_key)
            if cached is not None:
                elapsed = (time.monotonic() - start) * 1000
                context_latency_seconds.labels(type="warm").observe(elapsed / 1000)
                logger.debug(
                    "context.assembled",
                    org_id=str(self._org_id),
                    project_id=str(project_id),
                    query=query[:200],
                    cache_hit=True,
                    format=format,
                    as_of=as_of.isoformat() if as_of is not None else None,
                    assembly_time_ms=round(elapsed, 1),
                    source_counts={},
                    total_items=0,
                    context_length=len(cached),
                    top_episode=None,
                    top_fact=None,
                    query_embedding_dim=None,
                    configured_embedding_dim=(
                        self._retriever._org_config.embedding_dim
                        if self._retriever._org_config
                        else None
                    ),
                )
                return {
                    "context": cached,
                    "metadata": {
                        "cache_hit": True,
                        "assembly_time_ms": round(elapsed, 1),
                        "source_counts": {},
                        "total_items": 0,
                        "as_of": as_of,
                    },
                }

        # ═══════════════════════════════════════════════════════════════════
        # Step 2 — Run hybrid search
        # ═══════════════════════════════════════════════════════════════════
        results = await self._retriever.hybrid_search(
            query, project_id, limit, query_time=as_of
        )

        # ═══════════════════════════════════════════════════════════════════
        # Step 2b — Load blobs for returned episodes and generate presigned
        #           download URLs (best-effort, non-blocking on failure)
        # ═══════════════════════════════════════════════════════════════════
        episodes = results.get("episodes", [])
        if episodes:
            blob_repo = EpisodeBlobRepository(self._db)
            storage_config = (
                self._org_config.to_blob_storage_config()
                if self._org_config is not None
                else None
            )

            # Load blobs for all returned episodes concurrently (N+1 guard)
            episode_ids = [ep["id"] for ep in episodes if ep.get("id")]
            all_episode_blobs = await asyncio.gather(
                *[blob_repo.get_by_episode(eid) for eid in episode_ids],
            )
            # Index by episode id for O(1) lookup
            blobs_by_eid: dict[UUID, list[Any]] = {}
            for eid, blbs in zip(episode_ids, all_episode_blobs):
                if blbs:
                    blobs_by_eid[eid] = blbs

            for ep in episodes:
                eid = ep.get("id")
                if not eid or eid not in blobs_by_eid:
                    continue
                blbs = blobs_by_eid[eid]
                urls: list[str | None] = (
                    await asyncio.gather(*[
                        BlobStorageService.generate_download_url(
                            storage_key=bl.storage_key,
                            storage_config=storage_config,
                            expires_in=300,
                        )
                        for bl in blbs
                    ])
                    if storage_config
                    else [None] * len(blbs)
                )
                ep["blobs"] = [
                    {
                        "id": bl.id,
                        "file_name": bl.file_name,
                        "mime_type": bl.mime_type,
                        "file_size": bl.file_size,
                        "download_url": url,
                    }
                    for bl, url in zip(blbs, urls)
                ]

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
            query=query[:200],
            cache_hit=False,
            format=format,
            as_of=as_of.isoformat() if as_of is not None else None,
            assembly_time_ms=round(elapsed, 1),
            source_counts=results.get("source_counts", {}),
            total_items=results.get("total_items", 0),
            context_length=len(context_str),
            top_episode=_preview(results.get("episodes", [])),
            top_fact=_preview(results.get("facts", [])),
            query_embedding_dim=results.get("query_embedding_dim"),
            configured_embedding_dim=(
                self._retriever._org_config.embedding_dim
                if self._retriever._org_config
                else None
            ),
        )

        return {
            "context": context_str,
            "metadata": {
                "cache_hit": False,
                "assembly_time_ms": round(elapsed, 1),
                "source_counts": results["source_counts"],
                "total_items": results.get("total_items", 0),
                "as_of": as_of,
            },
        }
