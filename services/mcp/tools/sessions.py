"""Session tool — list_sessions.

Provides session listing for project conversation sessions.
"""

from __future__ import annotations

import logging
import time

from fastmcp import Context

from services.mcp.server import mcp

logger = logging.getLogger("openzync.mcp.tools.sessions")


@mcp.tool
async def list_sessions(
    ctx: Context,
    project_id: str,
    limit: int = 50,
    cursor: str | None = None,
) -> str:
    """List sessions for a project.

    Each session groups a sequence of conversation messages (episodes).
    Sessions are soft-deletable and have an optional external ID set by
    the caller.

    Pagination: pass the ``cursor`` value from a previous response to
    retrieve the next page.  When ``cursor`` is omitted, the first page
    is returned.

    Args:
        project_id: The internal UUID of the target project.
        limit: Maximum sessions to return per page (default 50, max 200).
        cursor: Opaque pagination cursor from a previous response.
            Omit to fetch the first page.

    Returns:
        A formatted string listing sessions with IDs and message counts,
        plus a pagination hint if more pages are available.
    """
    if not project_id:
        raise ValueError("project_id is required.")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200.")

    start = time.monotonic()
    logger.info("mcp.tool.invoke tool=%s project_id=%s limit=%d cursor=%s",
                "list_sessions", project_id, limit, cursor)

    client = ctx.lifespan_context["client"]
    try:
        result = await client.sessions.list(
            project_id=project_id,
            limit=limit,
            cursor=cursor,
        )

        sessions = result.get("data", result.get("items", []))
        next_cursor = result.get("next_cursor")
        has_more = result.get("has_more", False)

        elapsed = time.monotonic() - start
        logger.info("mcp.tool.success tool=%s duration_ms=%d session_count=%d has_more=%s",
                    "list_sessions", round(elapsed * 1000), len(sessions), has_more)

        if not sessions:
            return "No sessions found."

        lines = [f"Found {len(sessions)} session(s):"]
        for s in sessions:
            sid = (s.get("id") or "")[:8]
            ext = s.get("external_id", "")
            msgs = s.get("message_count", 0)
            lines.append(f"  [{sid}] {ext} ({msgs} messages)")

        # Append pagination hint
        if has_more and next_cursor:
            lines.append(f"\nMore sessions available. Use cursor=\"{next_cursor}\" for the next page.")

        return "\n".join(lines)
    except Exception:
        elapsed = time.monotonic() - start
        logger.error("mcp.tool.error tool=%s duration_ms=%d project_id=%s",
                     "list_sessions", round(elapsed * 1000), project_id, exc_info=True)
        raise
