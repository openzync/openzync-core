"""Memory tool handlers — add_memory, get_context, search_memory, delete_memory."""

from __future__ import annotations

import json
from typing import Any

from openzep.client import AsyncOpenZep


async def handle_add_memory(client: AsyncOpenZep, args: dict) -> dict:
    """Add messages to a user's memory."""
    user_id = args["user_id"]
    messages = args["messages"]
    session_id = args.get("session_id")

    response = await client.memory.ingest(
        user_id=user_id,
        messages=messages,
        session_id=session_id,
    )

    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Memory recorded. {response.episode_count} messages ingested "
                    f"(job: {response.job_id})."
                ),
            }
        ],
        "isError": False,
    }


async def handle_get_context(client: AsyncOpenZep, args: dict) -> dict:
    """Assemble a context block for LLM injection."""
    user_id = args["user_id"]
    query = args["query"]
    limit = args.get("limit", 20)

    response = await client.memory.get_context(
        user_id=user_id,
        query=query,
        limit=limit,
    )

    return {
        "content": [
            {
                "type": "text",
                "text": response.context,
            }
        ],
        "isError": False,
    }


async def handle_search_memory(client: AsyncOpenZep, args: dict) -> dict:
    """Search across a user's memory."""
    user_id = args["user_id"]
    query = args["query"]
    types = args.get("types", "episodes,facts")
    limit = args.get("limit", 20)

    results = await client.graph.search(
        user_id=user_id,
        query=query,
        types=types,
        limit=limit,
    )

    if not results:
        return {
            "content": [{"type": "text", "text": "No results found."}],
            "isError": False,
        }

    lines = [f"Found {len(results)} result(s):"]
    for r in results[:limit]:
        content = r.get("content", "")[:200]
        score = r.get("rrf_score", r.get("score", 0))
        lines.append(f"  [{score:.4f}] {content}")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "isError": False,
    }


async def handle_delete_memory(client: AsyncOpenZep, args: dict) -> dict:
    """Delete all memory for a user."""
    user_id = args["user_id"]

    await client.memory.delete(user_id=user_id)

    return {
        "content": [{"type": "text", "text": "Memory deleted successfully."}],
        "isError": False,
    }
