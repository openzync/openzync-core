"""Unit tests for the context assembly router.

Tests ``GET /v1/projects/{project_id}/context`` with query, limit, and format
parameters.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dependencies.db import get_db
from dependencies.org_config import get_org_config
from dependencies.project_auth import require_project_membership
from routers.context import router
from schemas.organization_config import OrgConfigBase

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


def _create_app() -> FastAPI:
    """Build a minimal FastAPI app with only the context router and overridden deps."""
    app = FastAPI()

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    # Set up app.state dependencies required by the router
    app.state.redis = AsyncMock()
    app.state.graph_backend_dispatcher = MagicMock()
    app.state.graph_backend_dispatcher.create_all_backends.return_value = []
    # Avoid SurrealDB connection path by setting graph_backend to postgres
    app.state.surreal_connection_pool = None

    mock_org_config = OrgConfigBase(graph_backend="postgres")
    app.dependency_overrides[get_org_config] = lambda: mock_org_config
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[require_project_membership] = lambda: None

    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_get_context_success_text() -> None:
    """GET with a valid query returns 200 with text-formatted context."""
    app = _create_app()
    transport = ASGITransport(app=app)

    assemble_result = {
        "context": "Assembled context block with relevant information.",
        "metadata": {
            "cache_hit": False,
            "assembly_time_ms": 12.5,
            "source_counts": {"episodes": 3, "facts": 5},
            "total_items": 8,
        },
    }

    with patch("routers.context.ContextService") as mock_service_class:
        mock_service = AsyncMock()
        mock_service_class.return_value = mock_service
        mock_service.assemble.return_value = assemble_result

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/v1/projects/{PROJECT_ID}/context",
                params={"query": "What happened in the last meeting?"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["context"] == "Assembled context block with relevant information."
    assert body["metadata"]["cache_hit"] is False
    assert body["metadata"]["total_items"] == 8
    assert resp.headers.get("x-cache") == "MISS"

    mock_service.assemble.assert_awaited_once()
    call_kwargs = mock_service.assemble.await_args[1]
    assert call_kwargs["project_id"] == PROJECT_ID
    assert call_kwargs["query"] == "What happened in the last meeting?"
    assert call_kwargs["limit"] == 20  # default
    assert call_kwargs["format"] == "text"  # default


@pytest.mark.asyncio
async def test_get_context_success_json() -> None:
    """GET with format=json returns 200 with JSON context."""
    app = _create_app()
    transport = ASGITransport(app=app)

    assemble_result = {
        "context": '{"sources": [{"type": "episode", "content": "..."}]}',
        "metadata": {
            "cache_hit": True,
            "assembly_time_ms": 0.8,
            "source_counts": {"episodes": 1},
            "total_items": 1,
        },
    }

    with patch("routers.context.ContextService") as mock_service_class:
        mock_service = AsyncMock()
        mock_service_class.return_value = mock_service
        mock_service.assemble.return_value = assemble_result

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/v1/projects/{PROJECT_ID}/context",
                params={"query": "summary", "limit": 5, "format": "json"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert "sources" in body["context"]
    assert body["metadata"]["cache_hit"] is True
    assert resp.headers.get("x-cache") == "HIT"

    mock_service.assemble.assert_awaited_once()
    call_kwargs = mock_service.assemble.await_args[1]
    assert call_kwargs["limit"] == 5
    assert call_kwargs["format"] == "json"


@pytest.mark.asyncio
async def test_get_context_422_no_query() -> None:
    """GET without a query parameter returns 422."""
    app = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/projects/{PROJECT_ID}/context")

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_context_422_empty_query() -> None:
    """GET with an empty query string returns 422."""
    app = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/context",
            params={"query": ""},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_context_422_invalid_format() -> None:
    """GET with an invalid format parameter returns 422."""
    app = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/context",
            params={"query": "test", "format": "xml"},
        )

    assert resp.status_code == 422
