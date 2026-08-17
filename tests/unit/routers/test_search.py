"""Unit tests for the /v1/projects/{project_id}/search router — HTTP adapter layer.

Tests the hybrid search endpoint with mocked dependencies, including
the graph_backend_dispatcher on app.state.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


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



@pytest.fixture(autouse=True)
def _stub_permission_gate() -> None:
    """Stub the permission gate for every test in this file.

    The router gates with ``require_permission("project:read")`` — a
    closure created at router import time that cannot be keyed in
    ``dependency_overrides``.  Patching ``dependencies.auth._check_permission``
    (the shared decision function) stubs the gate while keeping the
    ``require_org_id`` chain intact.  The real gate matrix is covered by
    ``test_admin_gate_matrix.py``.
    """
    with patch("dependencies.auth._check_permission", new=AsyncMock()):
        yield


class TestSearchRouter:
    """Full HTTP-adapter tests for the search router."""

    @pytest.fixture(autouse=True)
    def _setup_app(self) -> None:
        """Set up the FastAPI app with all dependency overrides."""
        from dependencies.db import get_db
        from dependencies.project_auth import require_project_membership
        from dependencies.org_config import get_org_config
        from routers.search import router
        from schemas.organization_config import OrgConfigBase

        # Mock graph_backend_dispatcher
        self.dispatcher_mock = MagicMock()
        self.dispatcher_mock.create_all_backends.return_value = []

        self.db_mock = AsyncMock()
        self.org_config_mock = OrgConfigBase(graph_backend="none")

        self.app = FastAPI()
        self.app.include_router(router)

        # Set up app.state before it's used
        self.app.state.graph_backend_dispatcher = self.dispatcher_mock

        self.app.dependency_overrides[get_db] = lambda: self.db_mock
        self.app.dependency_overrides[require_project_membership] = lambda: None
        self.app.dependency_overrides[get_org_config] = lambda: self.org_config_mock

        @self.app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

    @patch("routers.search.HybridRetriever")
    async def test_search_success(self, mock_hybrid: MagicMock) -> None:
        """GET /v1/projects/{project_id}/search?q=... → 200 with results."""
        mock_instance = MagicMock()
        mock_hybrid.return_value = mock_instance
        mock_instance.hybrid_search = AsyncMock(return_value={})

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/search",
                params={"query": "test query"},
            )

        # The hybrid retriever will run with no backends and no real DB,
        # so the search will return empty results. That's fine — the
        # endpoint itself reaches 200 and returns the expected shape.
        assert response.status_code == 200
        data = response.json()
        assert "query" in data
        assert "results" in data
        assert "total" in data
        assert data["query"] == "test query"
        assert isinstance(data["results"], list)

    async def test_search_422_empty_query(self) -> None:
        """GET /v1/projects/{project_id}/search without query → 422."""
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/search",
            )

        assert response.status_code == 422

    async def test_search_422_query_too_long(self) -> None:
        """GET /v1/projects/{project_id}/search with query > 2000 chars → 422."""
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/search",
                params={"query": "x" * 2001},
            )

        assert response.status_code == 422

    @patch("routers.search.HybridRetriever")
    async def test_search_with_type_filter(self, mock_hybrid: MagicMock) -> None:
        """GET .../search with types parameter → 200."""
        mock_instance = MagicMock()
        mock_hybrid.return_value = mock_instance
        mock_instance.hybrid_search = AsyncMock(return_value={})

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/search",
                params={"query": "test", "types": "episodes,facts"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "test"

    @patch("routers.search.HybridRetriever")
    async def test_search_with_limit(self, mock_hybrid: MagicMock) -> None:
        """GET .../search with limit → 200."""
        mock_instance = MagicMock()
        mock_hybrid.return_value = mock_instance
        mock_instance.hybrid_search = AsyncMock(return_value={})

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/search",
                params={"query": "test", "limit": 5},
            )

        assert response.status_code == 200

    async def test_search_422_invalid_limit(self) -> None:
        """GET .../search with limit out of range → 422."""
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/search",
                params={"query": "test", "limit": 999},
            )

        assert response.status_code == 422

    @patch("routers.search.HybridRetriever")
    async def test_search_includes_path_params(self, mock_hybrid: MagicMock) -> None:
        """Verify that path_params['project_id'] is available in the request handler."""
        mock_instance = MagicMock()
        mock_hybrid.return_value = mock_instance
        mock_instance.hybrid_search = AsyncMock(return_value={})

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/search",
                params={"query": "verify"},
            )

        assert response.status_code == 200
        # The route reads request.path_params["project_id"] — if it fails
        # we'd get a 500. 200 confirms it works.
