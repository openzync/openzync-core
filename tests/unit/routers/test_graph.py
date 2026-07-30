"""Unit tests for the /v1/projects/{project_id}/graph router — HTTP adapter layer.

Tests all 5 graph endpoints: list nodes, get node detail, delete node,
list edges, list communities.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from schemas.graph import (
    GraphNode,
    GraphNodeDetail,
    GraphNodeDetailResponse,
    GraphNodesListResponse,
    GraphEdgesListResponse,
    GraphCommunitiesListResponse,
    GraphEdge,
    GraphCommunity,
    PaginatedGraphNodes,
    PaginatedGraphEdges,
)

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
NODE_ID = UUID("00000000-0000-0000-0000-000000000010")


@pytest.fixture(autouse=True)
def _init_settings() -> None:
    """Initialise the Settings singleton with dummy values."""
    from core.config import Settings, set_settings

    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/test",
        REDIS_URL="redis://localhost:6379/1",
        SECRET_KEY="a" * 32,
        WEBHOOK_SIGNING_SECRET="b" * 32,
        ENVIRONMENT="test",
    )
    set_settings(settings)


class TestGraphRouter:
    """Full HTTP-adapter tests for the graph router."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        """Set up shared mocks and app for each test."""
        self.graph_service = AsyncMock()

        from dependencies.project_auth import require_project_membership
        from dependencies.services import get_graph_service
        from routers.graph import router

        self.app = FastAPI()
        self.app.include_router(router)

        self.app.dependency_overrides[require_project_membership] = lambda: None
        self.app.dependency_overrides[get_graph_service] = lambda: self.graph_service

        @self.app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

    # ── GET /v1/projects/{project_id}/graph/nodes ────────────────────────

    async def test_list_graph_nodes_success(self) -> None:
        """GET .../graph/nodes → 200 with GraphNodesListResponse."""
        self.graph_service.get_entities.return_value = {
            "items": [
                {
                    "id": str(NODE_ID),
                    "name": "Alice",
                    "type": "Person",
                    "summary": "A test person",
                    "created_at": "2026-01-01T00:00:00Z",
                    "metadata": {},
                },
            ],
            "next_cursor": None,
            "has_more": False,
        }

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/graph/nodes",
            )

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert data["data"]["items"][0]["name"] == "Alice"
        self.graph_service.get_entities.assert_awaited_once()

    async def test_list_graph_nodes_with_type_filter(self) -> None:
        """GET .../graph/nodes?entity_type=Person → 200."""
        self.graph_service.get_entities.return_value = {
            "items": [],
            "next_cursor": None,
            "has_more": False,
        }

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/graph/nodes",
                params={"entity_type": "Person"},
            )

        assert response.status_code == 200
        self.graph_service.get_entities.assert_awaited_once()
        call_kwargs = self.graph_service.get_entities.call_args.kwargs
        assert call_kwargs["entity_type"] == "Person"

    async def test_list_graph_nodes_with_session_filter(self) -> None:
        """GET .../graph/nodes?session_id=... → 200."""
        session_id = UUID("00000000-0000-0000-0000-000000000020")
        self.graph_service.get_entities.return_value = {
            "items": [],
            "next_cursor": None,
            "has_more": False,
        }

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/graph/nodes",
                params={"session_id": str(session_id)},
            )

        assert response.status_code == 200
        self.graph_service.get_entities.assert_awaited_once()

    # ── GET /v1/projects/{project_id}/graph/nodes/{node_id} ──────────────

    async def test_get_graph_node_success(self) -> None:
        """GET .../graph/nodes/{node_id} → 200 with GraphNodeDetailResponse."""
        self.graph_service.get_entity.return_value = {
            "node": {
                "id": str(NODE_ID),
                "name": "Alice",
                "type": "Person",
                "summary": "A test person",
                "created_at": "2026-01-01T00:00:00Z",
                "metadata": {},
            },
            "edges": [
                {
                    "id": str(UUID("00000000-0000-0000-0000-000000000030")),
                    "source_id": str(NODE_ID),
                    "target_id": str(UUID("00000000-0000-0000-0000-000000000040")),
                    "type": "works_at",
                    "properties": {},
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ],
        }

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/graph/nodes/{NODE_ID}",
            )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["node"]["name"] == "Alice"
        assert len(data["data"]["edges"]) == 1
        self.graph_service.get_entity.assert_awaited_once()

    async def test_get_graph_node_404_not_found(self) -> None:
        """GET .../graph/nodes/{node_id} when not found → 404."""
        from core.exceptions import NotFoundError, register_exception_handlers

        register_exception_handlers(self.app)
        self.graph_service.get_entity.side_effect = NotFoundError(
            message="Entity not found",
            detail={"entity_id": str(NODE_ID)},
        )

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/graph/nodes/{NODE_ID}",
            )

        assert response.status_code == 404

    # ── DELETE /v1/projects/{project_id}/graph/nodes/{node_id} ───────────

    async def test_delete_graph_node_success(self) -> None:
        """DELETE .../graph/nodes/{node_id} → 204 No Content."""
        self.graph_service.delete_entity.return_value = True

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete(
                f"/v1/projects/{PROJECT_ID}/graph/nodes/{NODE_ID}",
            )

        assert response.status_code == 204
        assert response.content == b""
        self.graph_service.delete_entity.assert_awaited_once()

    async def test_delete_graph_node_404_not_found(self) -> None:
        """DELETE .../graph/nodes/{node_id} when not found → 404."""
        from core.exceptions import register_exception_handlers

        register_exception_handlers(self.app)
        self.graph_service.delete_entity.return_value = False

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete(
                f"/v1/projects/{PROJECT_ID}/graph/nodes/{NODE_ID}",
            )

        assert response.status_code == 404

    # ── GET /v1/projects/{project_id}/graph/edges ────────────────────────

    async def test_list_graph_edges_success(self) -> None:
        """GET .../graph/edges?subject_id=... → 200 with GraphEdgesListResponse."""
        self.graph_service.get_edges.return_value = {
            "items": [
                {
                    "id": str(UUID("00000000-0000-0000-0000-000000000030")),
                    "source_id": str(NODE_ID),
                    "target_id": str(UUID("00000000-0000-0000-0000-000000000040")),
                    "type": "works_at",
                    "properties": {},
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ],
            "next_cursor": None,
            "has_more": False,
        }

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/graph/edges",
                params={"subject_id": str(NODE_ID)},
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]["items"]) == 1
        assert data["data"]["items"][0]["type"] == "works_at"
        self.graph_service.get_edges.assert_awaited_once()

    async def test_list_graph_edges_missing_subject_422(self) -> None:
        """GET .../graph/edges without subject_id → 422."""
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/graph/edges",
            )

        assert response.status_code == 422
        self.graph_service.get_edges.assert_not_called()

    async def test_list_graph_edges_with_subject_ids(self) -> None:
        """GET .../graph/edges?subject_ids=... → 200."""
        self.graph_service.get_edges.return_value = {
            "items": [],
            "next_cursor": None,
            "has_more": False,
        }

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/graph/edges",
                params={
                    "subject_ids": f"{NODE_ID},{UUID('00000000-0000-0000-0000-000000000040')}",
                },
            )

        assert response.status_code == 200
        self.graph_service.get_edges.assert_awaited_once()

    async def test_list_graph_edges_with_predicate(self) -> None:
        """GET .../graph/edges with predicate filter → 200."""
        self.graph_service.get_edges.return_value = {
            "items": [],
            "next_cursor": None,
            "has_more": False,
        }

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/graph/edges",
                params={"subject_id": str(NODE_ID), "predicate": "works_at"},
            )

        assert response.status_code == 200
        self.graph_service.get_edges.assert_awaited_once()
        call_kwargs = self.graph_service.get_edges.call_args.kwargs
        assert call_kwargs["predicate"] == "works_at"

    # ── GET /v1/projects/{project_id}/graph/communities ─────────────────

    async def test_list_communities_success(self) -> None:
        """GET .../graph/communities → 200 with GraphCommunitiesListResponse."""
        self.graph_service.get_communities.return_value = [
            {
                "id": str(UUID("00000000-0000-0000-0000-000000000050")),
                "name": "Engineering Cluster",
                "summary": "Engineers who work on the same project.",
                "member_count": 5,
                "created_at": "2026-01-01T00:00:00Z",
            },
        ]

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/graph/communities",
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["name"] == "Engineering Cluster"
        self.graph_service.get_communities.assert_awaited_once()

    async def test_list_communities_empty(self) -> None:
        """GET .../graph/communities when none exist → 200 with empty list."""
        self.graph_service.get_communities.return_value = []

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/graph/communities",
            )

        assert response.status_code == 200
        data = response.json()
        assert data["data"] == []
