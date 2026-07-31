"""Integration tests for graph query endpoints.

Endpoints under test (all under ``/v1/projects/{project_id}/graph``):

    GET    /v1/projects/{project_id}/graph/nodes             — List entity nodes
    GET    /v1/projects/{project_id}/graph/nodes/{node_id}   — Get single node with edges
    DELETE /v1/projects/{project_id}/graph/nodes/{node_id}   — Delete entity node
    GET    /v1/projects/{project_id}/graph/edges             — List relationship edges
    GET    /v1/projects/{project_id}/graph/communities       — List community summaries

The org is configured with ``graph_backend="postgres"`` via an org-config
dependency override on the isolated app.  On a fresh isolated project the
Postgres backend has no entities, so every endpoint returns empty results
and node lookups return 404 — these tests verify that behaviour end-to-end
through the HTTP layer.

Auth strategy:
    Each test uses the per-test isolation fixtures (``isolated_app`` +
    ``isolated_auth_client`` + ``isolated_project_id``), so no state leaks
    between tests.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
async def _graph_backend_env(isolated_app: Any) -> None:
    """Wire the graph backend dispatcher + postgres org-config override.

    The app lifespan (which normally sets ``graph_backend_dispatcher``) is
    not run in tests, and the stored org config has no ``graph_backend``
    value — without this fixture every graph endpoint would return 503.
    With ``graph_backend="postgres"``, endpoints hit the Postgres backend
    against the per-test database (empty → empty results, 404 lookups).
    """
    from core.graph_backend import init_dispatcher
    from dependencies.org_config import get_org_config
    from schemas.organization_config import OrgConfigBase

    isolated_app.state.graph_backend_dispatcher = init_dispatcher()
    isolated_app.dependency_overrides[get_org_config] = lambda: OrgConfigBase(
        graph_backend="postgres"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Tests — Graph Nodes
# ═══════════════════════════════════════════════════════════════════════════════


class TestGraphNodes:
    """Tests for ``GET /v1/projects/{project_id}/graph/nodes``."""

    @pytest.mark.asyncio
    async def test_list_nodes_returns_200(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /graph/nodes → 200 with empty items (fresh project)."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/graph/nodes",
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "data" in data
        assert "items" in data["data"]
        assert "next_cursor" in data["data"]
        assert "has_more" in data["data"]
        # Fresh project → no entities
        assert data["data"]["items"] == []
        assert data["data"]["has_more"] is False

    @pytest.mark.asyncio
    async def test_list_nodes_with_type_filter(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /graph/nodes?entity_type=Person → 200 with empty items."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/graph/nodes",
            params={"entity_type": "Person"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["items"] == []

    @pytest.mark.asyncio
    async def test_list_nodes_pagination_params(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /graph/nodes?limit=10&cursor=abc → 200 (cursor accepted gracefully)."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/graph/nodes",
            params={"limit": 10, "cursor": "eyJub2RlX2lkIjogImFiYyJ9"},  # base64 {"node_id": "abc"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["items"] == []


class TestGraphNodeDetail:
    """Tests for ``GET /v1/projects/{project_id}/graph/nodes/{node_id}``."""

    @pytest.mark.asyncio
    async def test_get_node_returns_404(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /graph/nodes/{id} → 404 for a non-existent entity."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/graph/nodes/"
            "00000000-0000-0000-0000-000000000001",
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_node_invalid_uuid(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /graph/nodes/{id} with invalid UUID → 422."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/graph/nodes/not-a-uuid",
        )
        assert resp.status_code == 422


class TestGraphDeleteNode:
    """Tests for ``DELETE /v1/projects/{project_id}/graph/nodes/{node_id}``."""

    @pytest.mark.asyncio
    async def test_delete_node_returns_404(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """DELETE /graph/nodes/{id} → 404 for a non-existent entity."""
        resp = await isolated_auth_client.delete(
            f"/v1/projects/{isolated_project_id}/graph/nodes/"
            "00000000-0000-0000-0000-000000000001",
        )
        assert resp.status_code == 404


class TestGraphEdges:
    """Tests for ``GET /v1/projects/{project_id}/graph/edges``."""

    @pytest.mark.asyncio
    async def test_list_edges_requires_subject(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /graph/edges without subject_id → 422."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/graph/edges",
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_list_edges_with_subject_returns_200(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /graph/edges?subject_id=... → 200 with empty items."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/graph/edges",
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
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /graph/edges?subject_id=...&predicate=works_at → 200."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/graph/edges",
            params={
                "subject_id": "00000000-0000-0000-0000-000000000001",
                "predicate": "works_at",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["items"] == []


class TestGraphCommunities:
    """Tests for ``GET /v1/projects/{project_id}/graph/communities``."""

    @pytest.mark.asyncio
    async def test_list_communities_returns_empty(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /graph/communities → 200 with empty list (no communities yet)."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/graph/communities",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert data["data"] == []


class TestGraphAuth:
    """Tests for graph endpoint auth enforcement."""

    @pytest.fixture
    async def anon_client(self, isolated_app: Any) -> Any:
        """Async client with no auth header, backed by the isolated app."""
        transport = ASGITransport(app=isolated_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    @pytest.mark.asyncio
    async def test_graph_requires_auth(self, anon_client: AsyncClient) -> None:
        """GET /graph/nodes without auth → 401."""
        resp = await anon_client.get(
            "/v1/projects/00000000-0000-0000-0000-000000000000/graph/nodes",
        )
        assert resp.status_code == 401
