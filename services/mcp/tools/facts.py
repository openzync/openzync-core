"""Fact tools — add_fact, list_facts.

These tools manage knowledge fact triples (subject-predicate-object)
extracted from or injected into a project's knowledge graph.
"""

from __future__ import annotations

import logging
import time

from fastmcp import Context

from services.mcp.server import mcp

logger = logging.getLogger("openzync.mcp.tools.facts")


@mcp.tool
async def add_fact(
    ctx: Context,
    project_id: str,
    facts: list[dict],
    session_id: str | None = None,
) -> str:
    """Add business fact triples to a project's knowledge graph.

    Each fact must be a triple with ``subject``, ``predicate``, and
    ``object`` keys.  Maximum 500 triples per call.

    Args:
        project_id: The internal UUID of the target project.
        facts: List of fact dicts, each with ``subject`` (str),
            ``predicate`` (str), ``object`` (str), and optional
            ``confidence`` (float, default 1.0).
        session_id: Optional session external ID for attribution.

    Returns:
        A confirmation message with accepted count and job ID.
    """
    if not facts:
        raise ValueError("At least one fact is required.")
    if len(facts) > 500:
        raise ValueError("Maximum 500 facts per call.")

    # Validate each fact has required keys
    for i, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise ValueError(f"Fact at index {i} must be a dict, got {type(fact).__name__}.")
        missing = {"subject", "predicate", "object"} - set(fact.keys())
        if missing:
            raise ValueError(f"Fact at index {i} is missing required key(s): {', '.join(sorted(missing))}.")

    start = time.monotonic()
    logger.info("mcp.tool.invoke tool=%s project_id=%s fact_count=%d session_id=%s",
                "add_fact", project_id, len(facts), session_id)

    client = ctx.lifespan_context["client"]
    try:
        response = await client.facts.add(
            project_id=project_id,
            facts=facts,
            session_id=session_id,
        )
        elapsed = time.monotonic() - start
        logger.info("mcp.tool.success tool=%s duration_ms=%d accepted_count=%d job_id=%s",
                    "add_fact", round(elapsed * 1000),
                    response.accepted_count, response.job_id)
        return (
            f"{response.accepted_count} fact(s) accepted for processing "
            f"(job: {response.job_id})."
        )
    except Exception:
        elapsed = time.monotonic() - start
        logger.error("mcp.tool.error tool=%s duration_ms=%d project_id=%s fact_count=%d",
                     "add_fact", round(elapsed * 1000), project_id, len(facts), exc_info=True)
        raise


@mcp.tool
async def list_facts(
    ctx: Context,
    project_id: str,
    query: str,
    limit: int = 20,
) -> str:
    """Search facts (knowledge triples) by keyword query.

    Searches across all facts in the project and returns matching
    results sorted by relevance.

    Args:
        project_id: The internal UUID of the target project.
        query: Keyword search query.
        limit: Maximum results (default 20, max 100).

    Returns:
        A formatted string of matching facts with confidence scores.
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string.")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100.")

    start = time.monotonic()
    logger.info("mcp.tool.invoke tool=%s project_id=%s query=%s limit=%d",
                "list_facts", project_id, query, limit)

    client = ctx.lifespan_context["client"]
    try:
        results = await client.graph.search(
            project_id=project_id,
            query=query,
            types="facts",
            limit=limit,
        )

        elapsed = time.monotonic() - start
        logger.info("mcp.tool.success tool=%s duration_ms=%d result_count=%d",
                    "list_facts", round(elapsed * 1000), len(results))

        if not results:
            return "No facts found."

        lines = [f"Found {len(results)} fact(s):"]
        for r in results[:limit]:
            content = (r.get("content") or "")[:200]
            confidence = r.get("confidence", 1.0)
            lines.append(f"  [{confidence:.2f}] {content}")

        return "\n".join(lines)
    except Exception:
        elapsed = time.monotonic() - start
        logger.error("mcp.tool.error tool=%s duration_ms=%d project_id=%s",
                     "list_facts", round(elapsed * 1000), project_id, exc_info=True)
        raise
