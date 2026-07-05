"""Graph tool — get_user_graph.

Provides access to the knowledge graph entity and relationship data
for a project.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastmcp import Context

from services.mcp.server import mcp

logger = logging.getLogger("openzync.mcp.tools.graph")

# Maximum number of entities to fetch edges for in parallel.
# Beyond this threshold, we only show entity summaries without edges.
_MAX_EDGE_SOURCES = 20


@mcp.tool
async def get_user_graph(
    ctx: Context,
    project_id: str,
    entity_type: str | None = None,
    limit: int = 50,
) -> str:
    """Get the entity graph for a project.

    Returns nodes (entities) and edges (relationships) from the
    knowledge graph.  Optionally filter by entity type.

    Edges are fetched in parallel for up to 20 entities to show
    inter-entity relationships.  For larger graphs, edges are shown
    only for the first 20 entities (use ``entity_type`` to narrow).
    Partial edge-fetch failures are handled gracefully — edges from
    failing entities are skipped with a warning logged.

    Args:
        project_id: The internal UUID of the target project.
        entity_type: Optional entity type filter (e.g. ``"Person"``,
            ``"Organization"``, ``"Topic"``).  When omitted, all
            entity types are returned.
        limit: Maximum entities and edges to return (default 50, max 200).

    Returns:
        A formatted string listing entities and their relationships.
    """
    if not project_id:
        raise ValueError("project_id is required.")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200.")

    start = time.monotonic()
    logger.info("mcp.tool.invoke tool=%s project_id=%s entity_type=%s limit=%d",
                "get_user_graph", project_id, entity_type, limit)

    client = ctx.lifespan_context["client"]
    try:
        # ── Collect entities ──────────────────────────────────────────────
        entities: list[dict[str, Any]] = []
        async for node in await client.graph.nodes(
            project_id=project_id,
            entity_type=entity_type,
            limit=limit,
        ):
            entities.append({
                "id": str(node.id),
                "name": node.name,
                "type": node.type,
                "summary": node.summary,
            })

        if not entities:
            elapsed = time.monotonic() - start
            logger.info("mcp.tool.success tool=%s duration_ms=%d entity_count=0",
                        "get_user_graph", round(elapsed * 1000))
            return "No entities found in the graph."

        # ── Collect edges for all entities in parallel ────────────────────
        # Cap source count to avoid overwhelming the API. Use
        # return_exceptions so a single timeout doesn't lose all edges.
        source_entities = entities[:_MAX_EDGE_SOURCES]

        edge_results = await asyncio.gather(*[
            client.graph.edges(
                project_id=project_id,
                subject_id=e["id"],
                limit=limit,
            )
            for e in source_entities
        ], return_exceptions=True)

        edges: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, str, str]] = set()  # dedup (source, target, type)
        failed_sources = 0
        for idx, result in enumerate(edge_results):
            if isinstance(result, Exception):
                failed_sources += 1
                logger.warning(
                    "mcp.tool.partial_failure tool=get_user_graph "
                    "entity_id=%s error=%s",
                    source_entities[idx]["id"], result,
                )
                continue
            async for edge in result:
                pair = (edge.source_id, edge.target_id, edge.type)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    edges.append({
                        "type": edge.type,
                        "source_id": edge.source_id,
                        "target_id": edge.target_id,
                    })
                    if len(edges) >= limit:
                        break
            if len(edges) >= limit:
                break

        elapsed = time.monotonic() - start

        logger.info(
            "mcp.tool.success tool=%s duration_ms=%d entity_count=%d edge_count=%d "
            "edge_sources_queried=%d edge_sources_failed=%d",
            "get_user_graph", round(elapsed * 1000),
            len(entities), len(edges),
            len(source_entities), failed_sources,
        )

        # ── Format output ─────────────────────────────────────────────────
        text_parts = [f"Found {len(entities)} entity(ies):"]
        for e in entities:
            text_parts.append(f"  [{e['type']}] {e['name']} ({e['id'][:8]}...)")

        if edges:
            note = f" (from {len(source_entities)} entity sources"
            if failed_sources:
                note += f", {failed_sources} skipped due to error"
            note += ")"
            text_parts.append(f"\n{len(edges)} edge(s){note}:")
            for ed in edges[: limit]:
                text_parts.append(
                    f"  [{ed['type']}] {ed['source_id'][:8]}... → {ed['target_id'][:8]}..."
                )
        else:
            text_parts.append("\nNo relationships found for the returned entities.")

        return "\n".join(text_parts)
    except Exception:
        elapsed = time.monotonic() - start
        logger.error("mcp.tool.error tool=%s duration_ms=%d project_id=%s entity_type=%s",
                     "get_user_graph", round(elapsed * 1000), project_id, entity_type,
                     exc_info=True)
        raise
