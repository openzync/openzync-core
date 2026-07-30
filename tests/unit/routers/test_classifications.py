"""Unit tests for the classification query router.

Tests ``GET /v1/projects/{project_id}/sessions/{session_id}/classifications``
and ``GET /.../classifications/{episode_id}``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dependencies.db import get_db
from dependencies.project_auth import require_project_membership
from routers.classifications import _get_classification_service, router
from schemas.classifications import ClassificationResponse

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
SESSION_ID = UUID("00000000-0000-0000-0000-000000000004")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000005")


@pytest.fixture
def mock_class_service() -> AsyncMock:
    """Return a fresh AsyncMock for the ClassificationService."""
    return AsyncMock()


def _create_app(mock_service: AsyncMock) -> FastAPI:
    """Build a minimal FastAPI app with only the classifications router."""
    app = FastAPI()

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[require_project_membership] = lambda: None
    app.dependency_overrides[_get_classification_service] = lambda: mock_service

    app.include_router(router)
    return app


NOW = datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_list_classifications_success() -> None:
    """GET list returns 200 with a list of classifications."""
    mock_service = AsyncMock()
    mock_service.get_classifications_for_session.return_value = [
        ClassificationResponse(
            id=UUID("00000000-0000-0000-0000-000000000010"),
            episode_id=EPISODE_ID,
            intent="inquiry",
            emotion="neutral",
            valence="positive",
            arousal="low",
            confidence=0.95,
            created_at=NOW,
            message="What is the price?",
            role="user",
        ),
    ]

    app = _create_app(mock_service)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}/classifications",
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["data"]) == 1
    assert body["data"][0]["intent"] == "inquiry"
    assert body["data"][0]["emotion"] == "neutral"
    assert body["data"][0]["confidence"] == 0.95

    mock_service.get_classifications_for_session.assert_awaited_once_with(
        org_id=ORG_ID,
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
    )


@pytest.mark.asyncio
async def test_list_classifications_empty() -> None:
    """GET list returns 200 with an empty list when no classifications exist."""
    mock_service = AsyncMock()
    mock_service.get_classifications_for_session.return_value = []

    app = _create_app(mock_service)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}/classifications",
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["data"] == []


@pytest.mark.asyncio
async def test_get_episode_classification_success() -> None:
    """GET by episode_id returns 200 with the classification."""
    mock_service = AsyncMock()
    mock_service.get_classification_for_episode.return_value = ClassificationResponse(
        id=UUID("00000000-0000-0000-0000-000000000010"),
        episode_id=EPISODE_ID,
        intent="complaint",
        emotion="frustrated",
        valence="negative",
        arousal="high",
        confidence=0.88,
        created_at=NOW,
        message="This is terrible!",
        role="user",
    )

    app = _create_app(mock_service)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}/classifications/{EPISODE_ID}",
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "complaint"
    assert body["emotion"] == "frustrated"
    assert body["episode_id"] == str(EPISODE_ID)

    mock_service.get_classification_for_episode.assert_awaited_once_with(
        org_id=ORG_ID,
        episode_id=EPISODE_ID,
    )


@pytest.mark.asyncio
async def test_get_episode_classification_404() -> None:
    """GET by episode_id returns 404 when no classification exists."""
    mock_service = AsyncMock()
    mock_service.get_classification_for_episode.return_value = None

    app = _create_app(mock_service)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}/classifications/{EPISODE_ID}",
        )

    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body
    assert str(EPISODE_ID) in body["detail"]
