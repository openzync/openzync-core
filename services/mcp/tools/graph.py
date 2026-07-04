"""Graph tool handler — get_user_graph."""

from __future__ import annotations

from typing import Any

from openzync.client import AsyncOpenZync


async def handle_get_user_graph(client: AsyncOpenZync, args: dict) -> dict:
    """Get the entity graph for a user."""
    user_id = args["user_id"]
    entity_type = args.get("entity_type")
    limit = args.get("limit", 50)

    # Collect entities
    entities: list[dict[str, Any]] = []
    async for node in await client.graph.nodes(
        user_id=user_id,
        entity_type=entity_type,
        limit=limit,
    ):
        entities.append({
            "id": node.id,
            "name": node.name,
            "type": node.type,
            "summary": node.summary,
        })

    if not entities:
        return {
            "content": [{"type": "text", "text": "No entities found in the graph."}],
            "isError": False,
        }

    # Collect edges for the first entity to show relationships
    edges: list[dict[str, str]] = []
    if entities:
        async for edge in await client.graph.edges(
            user_id=user_id,
            subject_id=entities[0]["id"],
            limit=limit,
        ):
            edges.append({
                "type": edge.type,
                "source_id": edge.source_id,
                "target_id": edge.target_id,
            })

    text_parts = [f"Found {len(entities)} entity(ies):"]
    for e in entities:
        text_parts.append(f"  [{e['type']}] {e['name']} ({e['id'][:8]}...)")

    if edges:
        text_parts.append(f"\n{len(edges)} edge(s):")
        for e in edges[:10]:
            text_parts.append(f"  [{e['type']}] {e['source_id'][:8]}... → {e['target_id'][:8]}...")
    else:
        text_parts.append("\nSelect an entity to see its relationships.")

    return {
        "content": [{"type": "text", "text": "\n".join(text_parts)}],
        "isError": False,
    }
