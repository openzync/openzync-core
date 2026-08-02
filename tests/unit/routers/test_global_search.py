"""Unit tests for the /v1/search router — global search endpoint.

Tests the GET /v1/search endpoint that searches across projects, users,
and sessions within the authenticated user's organisation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from schemas.search import GlobalSearchResponse, GlobalSearchItem

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


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


class TestGlobalSearchRouter:
    """Full HTTP-adapter tests for the global search router."""

    @patch("routers.global_search.GlobalSearchService")
    async def test_global_search_success(self, mock_search_service: AsyncMock) -> None:
        """GET /v1/search?q=... → 200 with GlobalSearchResponse."""
        mock_instance = AsyncMock()
        mock_search_service.return_value = mock_instance
        mock_instance.search.return_value = [
            GlobalSearchItem(
                type="project",
                id=str(UUID("00000000-0000-0000-0000-000000000010")),
                label="Test Project",
                subtitle="A test project",
                href="/projects/00000000-0000-0000-0000-000000000010",
            ),
            GlobalSearchItem(
                type="user",
                id=str(USER_ID),
                label="alice@example.com",
                subtitle="Alice Johnson",
                href="/users/alice@example.com",
            ),
        ]

        from dependencies.db import get_db
        from dependencies.auth import require_org_id, get_current_user_id
        from routers.global_search import router

        app = FastAPI()
        app.include_router(router)

        # ⚠️ Use lambda, not AsyncMock class directly — FastAPI 0.136.3+
        # on Python 3.14 introspects AsyncMock differently and produces
        # spurious 422 errors for "args" and "kwargs" parameters.
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/search", params={"q": "test"})

        assert response.status_code == 200
        data = response.json()
        assert "results" in data
        assert "query" in data
        assert data["query"] == "test"
        assert len(data["results"]) == 2
        assert data["results"][0]["type"] == "project"
        assert data["results"][1]["type"] == "user"
        mock_instance.search.assert_awaited_once()

    @patch("routers.global_search.GlobalSearchService")
    async def test_global_search_empty_query_422(
        self, mock_search_service: AsyncMock,
    ) -> None:
        """GET /v1/search without query parameter → 422."""
        mock_instance = AsyncMock()
        mock_search_service.return_value = mock_instance

        from dependencies.db import get_db
        from dependencies.auth import require_org_id, get_current_user_id
        from routers.global_search import router

        app = FastAPI()
        app.include_router(router)
        # ⚠️ Use lambda, not AsyncMock class directly — FastAPI 0.136.3+
        # on Python 3.14 introspects AsyncMock differently and produces
        # spurious 422 errors for "args" and "kwargs" parameters.
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/search")

        assert response.status_code == 422
        mock_instance.search.assert_not_called()

    @patch("routers.global_search.GlobalSearchService")
    async def test_global_search_empty_q_422(
        self, mock_search_service: AsyncMock,
    ) -> None:
        """GET /v1/search?q= with empty value → 422."""
        mock_instance = AsyncMock()
        mock_search_service.return_value = mock_instance

        from dependencies.db import get_db
        from dependencies.auth import require_org_id, get_current_user_id
        from routers.global_search import router

        app = FastAPI()
        app.include_router(router)
        # ⚠️ Use lambda, not AsyncMock class directly — FastAPI 0.136.3+
        # on Python 3.14 introspects AsyncMock differently and produces
        # spurious 422 errors for "args" and "kwargs" parameters.
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/search", params={"q": ""})

        assert response.status_code == 422
        mock_instance.search.assert_not_called()

    @patch("routers.global_search.GlobalSearchService")
    async def test_global_search_empty_results(
        self, mock_search_service: AsyncMock,
    ) -> None:
        """GET /v1/search?q=xyz with no matches → 200 with empty results."""
        mock_instance = AsyncMock()
        mock_search_service.return_value = mock_instance
        mock_instance.search.return_value = []

        from dependencies.db import get_db
        from dependencies.auth import require_org_id, get_current_user_id
        from routers.global_search import router

        app = FastAPI()
        app.include_router(router)
        # ⚠️ Use lambda, not AsyncMock class directly — FastAPI 0.136.3+
        # on Python 3.14 introspects AsyncMock differently and produces
        # spurious 422 errors for "args" and "kwargs" parameters.
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/search", params={"q": "xyznonexistent"})

        assert response.status_code == 200
        data = response.json()
        assert data["results"] == []
        assert data["query"] == "xyznonexistent"
        mock_instance.search.assert_awaited_once()

    @patch("routers.global_search.GlobalSearchService")
    async def test_global_search_with_limit(
        self, mock_search_service: AsyncMock,
    ) -> None:
        """GET /v1/search with custom limit → 200."""
        mock_instance = AsyncMock()
        mock_search_service.return_value = mock_instance
        mock_instance.search.return_value = []

        from dependencies.db import get_db
        from dependencies.auth import require_org_id, get_current_user_id
        from routers.global_search import router

        app = FastAPI()
        app.include_router(router)
        # ⚠️ Use lambda, not AsyncMock class directly — FastAPI 0.136.3+
        # on Python 3.14 introspects AsyncMock differently and produces
        # spurious 422 errors for "args" and "kwargs" parameters.
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/search", params={"q": "test", "limit": 5})

        assert response.status_code == 200
        mock_instance.search.assert_awaited_once()
        # Verify limit was passed to search
        call_kwargs = mock_instance.search.call_args.kwargs
        assert call_kwargs["limit"] == 5
