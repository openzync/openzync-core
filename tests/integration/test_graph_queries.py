"""Integration tests for graph query endpoints.

Endpoints under test:

    GET    /v1/users/{user_id}/graph/nodes             — List entity nodes
    GET    /v1/users/{user_id}/graph/nodes/{node_id}   — Get single node with edges
    DELETE /v1/users/{user_id}/graph/nodes/{node_id}   — Delete entity node
    GET    /v1/users/{user_id}/graph/edges             — List relationship edges
    GET    /v1/users/{user_id}/graph/communities       — List community summaries

All graph endpoints gracefully return empty results when the graph backend
(Graphiti / FalkorDB) is not available.  These tests verify that behaviour.

Auth strategy:
    Each test creates a fresh org via the admin bootstrap fixture and
    uses ``auth_client`` (pre-authenticated) for all authenticated calls.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


# ═══════════════════════════════════════════════════════════════════════════════
# Tests — Graph Nodes
# ═══════════════════════════════════════════════════════════════════════════════


class TestGraphNodes:
    """Tests for ``GET /v1/users/{user_id}/graph/nodes``."""

    @pytest.mark.asyncio
    async def test_list_nodes_returns_200(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /graph/nodes → 200 with empty items (no graph backend)."""
        # Create a user first
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "graph_nodes_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # List graph nodes
        resp = await auth_client.get(
            f"/v1/users/{user_id}/graph/nodes",
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "data" in data
        assert "items" in data["data"]
        assert "next_cursor" in data["data"]
        assert "has_more" in data["data"]
        # With no graph backend, items should be empty
        assert data["data"]["items"] == []
        assert data["data"]["has_more"] is False

    @pytest.mark.asyncio
    async def test_list_nodes_with_type_filter(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /graph/nodes?entity_type=Person → 200 with empty items."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "graph_nodes_type_filter"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.get(
            f"/v1/users/{user_id}/graph/nodes",
            params={"entity_type": "Person"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["items"] == []

    @pytest.mark.asyncio
    async def test_list_nodes_pagination_params(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /graph/nodes?limit=10&cursor=abc → 200 (cursor accepted gracefully)."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "graph_nodes_pagination"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.get(
            f"/v1/users/{user_id}/graph/nodes",
            params={"limit": 10, "cursor": "eyJub2RlX2lkIjogImFiYyJ9"},  # base64 {"node_id": "abc"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["items"] == []


class TestGraphNodeDetail:
    """Tests for ``GET /v1/users/{user_id}/graph/nodes/{node_id}``."""

    @pytest.mark.asyncio
    async def test_get_node_returns_404(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /graph/nodes/{id} → 404 when graph backend not available."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "graph_detail_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.get(
            f"/v1/users/{user_id}/graph/nodes/00000000-0000-0000-0000-000000000001",
        )
        # When graph backend is unavailable, entity is not found
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_node_invalid_uuid(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /graph/nodes/{id} with invalid UUID → 422."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "graph_invalid_uuid"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.get(
            f"/v1/users/{user_id}/graph/nodes/not-a-uuid",
        )
        assert resp.status_code == 422


class TestGraphDeleteNode:
    """Tests for ``DELETE /v1/users/{user_id}/graph/nodes/{node_id}``."""

    @pytest.mark.asyncio
    async def test_delete_node_returns_404(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """DELETE /graph/nodes/{id} → 404 when graph backend not available."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "graph_delete_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.delete(
            f"/v1/users/{user_id}/graph/nodes/00000000-0000-0000-0000-000000000001",
        )
        assert resp.status_code == 404


class TestGraphEdges:
    """Tests for ``GET /v1/users/{user_id}/graph/edges``."""

    @pytest.mark.asyncio
    async def test_list_edges_requires_subject(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /graph/edges without subject_id → 422."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "graph_edges_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.get(
            f"/v1/users/{user_id}/graph/edges",
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_list_edges_with_subject_returns_200(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /graph/edges?subject_id=... → 200 with empty items."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "graph_edges_subject"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.get(
            f"/v1/users/{user_id}/graph/edges",
            params={"subject_id": "00000000-0000-0000-0000-000000000001"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert "items" in data["data"]
        assert data["data"]["items"] == []

    @pytest.mark.asyncio
    async def test_list_edges_with_predicate_filter(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /graph/edges?subject_id=...&predicate=works_at → 200."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "graph_edges_predicate"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.get(
            f"/v1/users/{user_id}/graph/edges",
            params={
                "subject_id": "00000000-0000-0000-0000-000000000001",
                "predicate": "works_at",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["items"] == []


class TestGraphCommunities:
    """Tests for ``GET /v1/users/{user_id}/graph/communities``."""

    @pytest.mark.asyncio
    async def test_list_communities_returns_empty(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /graph/communities → 200 with empty list (not yet implemented)."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "graph_communities_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.get(
            f"/v1/users/{user_id}/graph/communities",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert data["data"] == []


class TestGraphAuth:
    """Tests for graph endpoint auth enforcement."""

    @pytest.mark.asyncio
    async def test_graph_requires_auth(
        self,
        anon_client: pytest.fixture,  # noqa: ARG002
    ) -> None:
        """GET /graph/nodes without auth → 401."""
        resp = await anon_client.get(  # type: ignore[union-attr]
            "/v1/users/00000000-0000-0000-0000-000000000001/graph/nodes",
        )
        assert resp.status_code == 401
