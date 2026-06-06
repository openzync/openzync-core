"""Tests for the OpenZep Python SDK — graph domain."""

from __future__ import annotations

import pytest


class TestGraphClient:
    """Tests for ``AsyncGraphClient``."""

    @pytest.mark.asyncio
    async def test_list_nodes(self, async_client, mock_http):
        """GET /graph/nodes returns paginated entities."""
        user_id = "user-123"
        mock_http.get(f"/v1/users/{user_id}/graph/nodes").respond(json={
            "data": {
                "items": [
                    {"id": "n1", "name": "Alice", "type": "Person", "summary": "",
                     "created_at": "2026-01-01T00:00:00Z", "metadata": {}},
                    {"id": "n2", "name": "Acme Corp", "type": "Organization", "summary": "",
                     "created_at": "2026-01-01T00:00:00Z", "metadata": {}},
                ],
                "next_cursor": None,
                "has_more": False,
            }
        })

        nodes = []
        async for node in await async_client.graph.nodes(user_id=user_id):
            nodes.append(node)

        assert len(nodes) == 2
        assert nodes[0].name == "Alice"
        assert nodes[1].type == "Organization"

    @pytest.mark.asyncio
    async def test_search(self, async_client, mock_http):
        """GET /search returns results."""
        user_id = "user-123"
        mock_http.get(f"/v1/users/{user_id}/search").respond(json={
            "query": "Alice",
            "results": [
                {"id": "e1", "content": "Alice works at Acme Corp", "score": 0.06,
                 "rrf_score": 0.03, "role": "user", "created_at": "2026-01-01T00:00:00Z"},
            ],
            "total": 1,
        })

        results = await async_client.graph.search(user_id=user_id, query="Alice")
        assert len(results) == 1
        assert "Acme Corp" in results[0]["content"]
