"""Unit tests for the observation query router.

Tests ``GET /v1/projects/{project_id}/observations`` with optional
filters (subject_entity_id, observation_type, limit).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dependencies.project_auth import require_project_membership
from dependencies.services import get_graph_backend_for_project
from routers.observations import router
from schemas.observation import ObservationListResponse, ObservationResponse

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


MOCK_GRAPH_BACKEND = AsyncMock()
"""Shared mock GraphBackend instance reused across all tests and DI resolution."""


def _create_app() -> FastAPI:
    """Build a minimal FastAPI app with only the observations router."""
    app = FastAPI()

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    app.dependency_overrides[require_project_membership] = lambda: None
    app.dependency_overrides[get_graph_backend_for_project] = lambda: MOCK_GRAPH_BACKEND

    app.include_router(router)
    return app


@pytest.fixture(autouse=True)
def _reset_mock() -> None:
    """Reset the shared mock before each test."""
    MOCK_GRAPH_BACKEND.reset_mock()
    MOCK_GRAPH_BACKEND.get_observations = AsyncMock()
    MOCK_GRAPH_BACKEND.resolve_entity_names = AsyncMock(return_value={})


NOW = datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_list_observations_success() -> None:
    """GET returns 200 with a list of observations."""
    entity_id = UUID("00000000-0000-0000-0000-000000000020")
    MOCK_GRAPH_BACKEND.get_observations.return_value = {
        "items": [
            {
                "id": str(UUID("00000000-0000-0000-0000-000000000010")),
                "organization_id": str(ORG_ID),
                "project_id": str(PROJECT_ID),
                "subject_entity_id": str(entity_id),
                "related_entity_id": None,
                "observation_type": "temporal_pattern",
                "content": "Entity appears frequently in morning sessions.",
                "supporting_fact_ids": None,
                "supporting_relationship_ids": None,
                "confidence": 0.85,
                "valid_from": None,
                "valid_to": None,
                "observation_metadata": None,
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "subject_entity_name": "Alice",
                "related_entity_name": None,
            },
        ],
        "next_cursor": None,
        "has_more": False,
    }
    MOCK_GRAPH_BACKEND.resolve_entity_names.return_value = {
        str(entity_id): {"name": "Alice", "type": "user"},
    }

    app = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/observations",
            params={"observation_type": "temporal_pattern", "limit": 10},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["data"]) == 1
    assert body["data"][0]["observation_type"] == "temporal_pattern"
    assert body["data"][0]["confidence"] == 0.85
    assert body["data"][0]["subject_entity_name"] == "Alice"

    MOCK_GRAPH_BACKEND.get_observations.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_observations_empty() -> None:
    """GET returns 200 with an empty list when no observations match."""
    MOCK_GRAPH_BACKEND.get_observations.return_value = {
        "items": [],
        "next_cursor": None,
        "has_more": False,
    }

    app = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/observations",
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["data"] == []


@pytest.mark.asyncio
async def test_list_observations_with_entity_filter() -> None:
    """GET with subject_entity_id filter returns filtered results."""
    entity_id = UUID("00000000-0000-0000-0000-000000000020")
    MOCK_GRAPH_BACKEND.get_observations.return_value = {
        "items": [
            {
                "id": str(UUID("00000000-0000-0000-0000-000000000010")),
                "organization_id": str(ORG_ID),
                "project_id": str(PROJECT_ID),
                "subject_entity_id": str(entity_id),
                "related_entity_id": None,
                "observation_type": "co_occurrence",
                "content": "Co-occurs with Bob.",
                "supporting_fact_ids": [],
                "supporting_relationship_ids": [],
                "confidence": 0.92,
                "valid_from": None,
                "valid_to": None,
                "observation_metadata": {},
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "subject_entity_name": "Alice",
                "related_entity_name": "Bob",
            },
        ],
        "next_cursor": None,
        "has_more": False,
    }
    MOCK_GRAPH_BACKEND.resolve_entity_names.return_value = {
        str(entity_id): {"name": "Alice", "type": "user"},
    }

    app = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/observations",
            params={"subject_entity_id": str(entity_id)},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["data"][0]["subject_entity_id"] == str(entity_id)

    MOCK_GRAPH_BACKEND.get_observations.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_observations_422_invalid_limit() -> None:
    """GET with an out-of-range limit returns 422."""
    app = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/observations",
            params={"limit": 0},
        )

    assert resp.status_code == 422
