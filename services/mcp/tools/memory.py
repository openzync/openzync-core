"""Memory tools — add_memory, get_context, search_memory, delete_memory.

These tools provide the core memory operations: ingest messages, retrieve
context for LLM prompting, hybrid search, and full memory wipe.
"""

from __future__ import annotations

import logging
import time

from fastmcp import Context

from services.mcp.server import mcp

logger = logging.getLogger("openzync.mcp.tools.memory")


@mcp.tool
async def add_memory(
    ctx: Context,
    project_id: str,
    messages: list[dict],
    session_id: str | None = None,
) -> str:
    """Add messages to a project's memory.

    Messages are persisted immediately as episodes in PostgreSQL and
    queued for async enrichment (entity extraction, fact extraction,
    embedding, classification, and structured extraction).

    Args:
        project_id: The internal UUID of the target project.
        messages: List of message objects, each with ``role``
            (``"user"`` | ``"assistant"`` | ``"system"`` | ``"tool"``)
            and ``content`` (message body text).  At least 1 message
            required, maximum 1000.
        session_id: Optional session external ID.  If omitted, a
            ``__default__`` session is auto-created for the project.

    Returns:
        A confirmation message with the job ID and episode count.
    """
    if not messages:
        raise ValueError("At least one message is required.")
    if len(messages) > 1000:
        raise ValueError("Maximum 1000 messages per call.")

    start = time.monotonic()
    logger.info("mcp.tool.invoke tool=%s project_id=%s message_count=%d session_id=%s",
                "add_memory", project_id, len(messages), session_id)

    client = ctx.lifespan_context["client"]
    try:
        response = await client.memory.ingest(
            project_id=project_id,
            messages=messages,
            session_id=session_id,
        )
        elapsed = time.monotonic() - start
        logger.info("mcp.tool.success tool=%s duration_ms=%d episode_count=%d job_id=%s",
                    "add_memory", round(elapsed * 1000),
                    response.episode_count, response.job_id)
        return (
            f"Memory recorded. {response.episode_count} messages ingested "
            f"(job: {response.job_id})."
        )
    except Exception:
        elapsed = time.monotonic() - start
        logger.error("mcp.tool.error tool=%s duration_ms=%d project_id=%s",
                     "add_memory", round(elapsed * 1000), project_id, exc_info=True)
        raise


@mcp.tool
async def get_context(
    ctx: Context,
    project_id: str,
    query: str,
    limit: int = 20,
) -> str:
    """Assemble a context block for LLM injection from a natural-language query.

    Returns recent episodes, extracted facts, and graph entities related
    to the query.  The context is assembled via hybrid search (vector +
    BM25 + graph traversal, fused via RRF) and formatted as plain text
    suitable for inclusion in an LLM prompt.

    Args:
        project_id: The internal UUID of the target project.
        query: A natural-language query describing the needed context
            (e.g. "what does the user know about machine learning").
        limit: Maximum items per source type (1–100, default 20).

    Returns:
        The assembled context text block.
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string.")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100.")

    start = time.monotonic()
    logger.info("mcp.tool.invoke tool=%s project_id=%s query_length=%d limit=%d",
                "get_context", project_id, len(query), limit)

    client = ctx.lifespan_context["client"]
    try:
        response = await client.memory.get_context(
            project_id=project_id,
            query=query,
            limit=limit,
        )
        elapsed = time.monotonic() - start
        logger.info("mcp.tool.success tool=%s duration_ms=%d context_length=%d",
                    "get_context", round(elapsed * 1000), len(response.context))
        return response.context
    except Exception:
        elapsed = time.monotonic() - start
        logger.error("mcp.tool.error tool=%s duration_ms=%d project_id=%s",
                     "get_context", round(elapsed * 1000), project_id, exc_info=True)
        raise


@mcp.tool
async def search_memory(
    ctx: Context,
    project_id: str,
    query: str,
    types: str = "episodes,facts",
    limit: int = 20,
) -> str:
    """Search across a project's memory using hybrid retrieval.

    Searches episodes, facts, and optionally entities matching the query.
    Results are fused via RRF and returned sorted by relevance score.

    Args:
        project_id: The internal UUID of the target project.
        query: Search query string.
        types: Comma-separated result types to include:
            ``"episodes"``, ``"facts"``, ``"entities"``
            (default: ``"episodes,facts"``).
        limit: Maximum results per type (default 20, max 100).

    Returns:
        A formatted string of search results with relevance scores.
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string.")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100.")

    # Validate types against allowed values
    allowed_types = {"episodes", "facts", "entities"}
    requested = {t.strip() for t in types.split(",")}
    if not requested:
        raise ValueError("types must be a non-empty comma-separated list.")
    invalid = requested - allowed_types
    if invalid:
        raise ValueError(f"Invalid type(s): {', '.join(sorted(invalid))}. "
                         f"Allowed: {', '.join(sorted(allowed_types))}.")

    start = time.monotonic()
    logger.info("mcp.tool.invoke tool=%s project_id=%s query=%s types=%s limit=%d",
                "search_memory", project_id, query, types, limit)

    client = ctx.lifespan_context["client"]
    try:
        results = await client.graph.search(
            project_id=project_id,
            query=query,
            types=types,
            limit=limit,
        )

        elapsed = time.monotonic() - start
        logger.info("mcp.tool.success tool=%s duration_ms=%d result_count=%d",
                    "search_memory", round(elapsed * 1000), len(results))

        if not results:
            return "No results found."

        lines = [f"Found {len(results)} result(s):"]
        for r in results[:limit]:
            content = (r.get("content") or "")[:200]
            score = r.get("rrf_score", r.get("score", 0))
            lines.append(f"  [{score:.4f}] {content}")

        return "\n".join(lines)
    except Exception:
        elapsed = time.monotonic() - start
        logger.error("mcp.tool.error tool=%s duration_ms=%d project_id=%s",
                     "search_memory", round(elapsed * 1000), project_id, exc_info=True)
        raise


@mcp.tool
async def delete_memory(ctx: Context, project_id: str) -> str:
    """Delete all memory for a project (soft-delete).

    Soft-deletes all episodes (messages) and facts for the project.
    Sessions remain intact.  This is the GDPR memory-wipe operation
    and is **not** reversible — deleted data is marked inactive but
    preserved for a 30-day grace period before hard-purge.

    Args:
        project_id: The internal UUID of the target project.

    Returns:
        A confirmation message.
    """
    if not project_id:
        raise ValueError("project_id is required.")

    start = time.monotonic()
    logger.info("mcp.tool.invoke tool=%s project_id=%s",
                "delete_memory", project_id)

    client = ctx.lifespan_context["client"]
    try:
        await client.memory.delete(project_id=project_id)
        elapsed = time.monotonic() - start
        logger.info("mcp.tool.success tool=%s duration_ms=%d",
                    "delete_memory", round(elapsed * 1000))
        return "Memory deleted successfully."
    except Exception:
        elapsed = time.monotonic() - start
        logger.error("mcp.tool.error tool=%s duration_ms=%d project_id=%s",
                     "delete_memory", round(elapsed * 1000), project_id, exc_info=True)
        raise
