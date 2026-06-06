"""Graph domain client — nodes, edges, communities, search."""

from __future__ import annotations

from typing import Any

from openzep._http import AsyncHTTPTransport
from openzep._pagination import AsyncPaginatedIterator
from openzep.models.graph import (
    GraphCommunity,
    GraphEdge,
    GraphNode,
    GraphNodeDetail,
)


class AsyncGraphClient:
    """Async client for knowledge graph operations.

    Args:
        http: The shared async HTTP transport instance.
    """

    def __init__(self, http: AsyncHTTPTransport) -> None:
        self._http = http

    async def nodes(
        self,
        user_id: str,
        *,
        entity_type: str | None = None,
        limit: int = 50,
    ) -> AsyncPaginatedIterator:
        """List entity nodes with optional type filter.

        Returns an async iterator that auto-fetches subsequent pages.
        Yields ``GraphNode`` objects.
        """
        async def fetch_page(cursor: str | None = None) -> dict:
            params: dict[str, str | int] = {"limit": limit}
            if entity_type is not None:
                params["entity_type"] = entity_type
            if cursor is not None:
                params["cursor"] = cursor
            raw = await self._http.request(
                "GET",
                f"/v1/users/{user_id}/graph/nodes",
                params=params,
            )
            # API wraps items in data.items — flatten for paginator
            data = raw.get("data", raw)
            items = data.get("items", [])
            return {
                "items": [GraphNode(**i) for i in items],
                "next_cursor": data.get("next_cursor"),
                "has_more": data.get("has_more", False),
            }

        return AsyncPaginatedIterator(fetch_page, limit)

    async def node_detail(
        self,
        user_id: str,
        node_id: str,
    ) -> GraphNodeDetail:
        """Get a single entity node with all its incident edges."""
        data = await self._http.request(
            "GET",
            f"/v1/users/{user_id}/graph/nodes/{node_id}",
        )
        inner = data.get("data", data)
        return GraphNodeDetail(
            node=GraphNode(**inner["node"]),
            edges=[GraphEdge(**e) for e in inner.get("edges", [])],
        )

    async def delete_node(self, user_id: str, node_id: str) -> None:
        """Delete an entity node from the knowledge graph."""
        await self._http.request(
            "DELETE",
            f"/v1/users/{user_id}/graph/nodes/{node_id}",
        )

    async def edges(
        self,
        user_id: str,
        subject_id: str,
        *,
        predicate: str | None = None,
        limit: int = 50,
    ) -> AsyncPaginatedIterator:
        """List relationship edges for a specific entity."""
        async def fetch_page(cursor: str | None = None) -> dict:
            params: dict[str, str | int] = {"subject_id": subject_id, "limit": limit}
            if predicate is not None:
                params["predicate"] = predicate
            if cursor is not None:
                params["cursor"] = cursor
            raw = await self._http.request(
                "GET",
                f"/v1/users/{user_id}/graph/edges",
                params=params,
            )
            data = raw.get("data", raw)
            return {
                "items": data.get("items", []),
                "next_cursor": data.get("next_cursor"),
                "has_more": data.get("has_more", False),
            }

        return AsyncPaginatedIterator(fetch_page, limit)

    async def communities(
        self,
        user_id: str,
    ) -> list[GraphCommunity]:
        """List community summary nodes."""
        data = await self._http.request(
            "GET",
            f"/v1/users/{user_id}/graph/communities",
        )
        items = data.get("data", [])
        return [GraphCommunity(**c) for c in items]

    async def search(
        self,
        user_id: str,
        query: str,
        *,
        types: str = "episodes,facts",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Hybrid search across user memory.

        Args:
            user_id: The internal UUID of the user.
            query: Search query string.
            types: Comma-separated result types (episodes, facts, entities).
            limit: Maximum results per type.

        Returns:
            List of result dicts with ``content``, ``score``, etc.
        """
        data = await self._http.request(
            "GET",
            f"/v1/users/{user_id}/search",
            params={"query": query, "types": types, "limit": str(limit)},
        )
        return data.get("results", [])

