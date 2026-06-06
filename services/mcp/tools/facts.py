"""Fact tool handlers — add_fact, list_facts."""

from __future__ import annotations

from typing import Any

from openzep.client import AsyncOpenZep


async def handle_add_fact(client: AsyncOpenZep, args: dict) -> dict:
    """Add business fact triples to a user's knowledge graph."""
    user_id = args["user_id"]
    facts = args["facts"]
    session_id = args.get("session_id")

    response = await client.facts.add(
        user_id=user_id,
        facts=facts,
        session_id=session_id,
    )

    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"{response.accepted_count} fact(s) accepted for processing "
                    f"(job: {response.job_id})."
                ),
            }
        ],
        "isError": False,
    }


async def handle_list_facts(client: AsyncOpenZep, args: dict) -> dict:
    """Search facts by keyword query."""
    user_id = args["user_id"]
    query = args["query"]
    limit = args.get("limit", 20)

    results = await client.graph.search(
        user_id=user_id,
        query=query,
        types="facts",
        limit=limit,
    )

    if not results:
        return {
            "content": [{"type": "text", "text": "No facts found."}],
            "isError": False,
        }

    lines = [f"Found {len(results)} fact(s):"]
    for r in results[:limit]:
        content = r.get("content", "")[:200]
        confidence = r.get("confidence", 1.0)
        lines.append(f"  [{confidence:.2f}] {content}")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "isError": False,
    }
