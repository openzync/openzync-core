"""Unit tests for the structured extraction query router.

Tests ``GET /v1/projects/{project_id}/sessions/{session_id}/structured-extractions``
and ``GET /.../structured-extractions/{episode_id}``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dependencies.db import get_db
from dependencies.project_auth import require_project_membership
from routers.structured_extractions import _get_extraction_service, router
from schemas.structured_extractions import (
    StructuredExtractionListResponse,
    StructuredExtractionResponse,
)

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
SESSION_ID = UUID("00000000-0000-0000-0000-000000000004")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000005")



@pytest.fixture(autouse=True)
def _stub_permission_gate() -> None:
    """Stub the permission gate for every test in this file.

    The router gates with ``require_permission("project:read")`` /
    ``require_permission("project:write")`` — closures created at router
    import time that cannot be keyed in ``dependency_overrides``.  Patching
    ``dependencies.auth._check_permission`` (the shared decision function)
    stubs the gate while keeping the ``require_org_id`` chain intact.  The
    real gate matrix is covered by ``test_admin_gate_matrix.py``.
    """
    with patch("dependencies.auth._check_permission", new=AsyncMock()):
        yield


def _create_app() -> FastAPI:
    """Build a minimal FastAPI app with only the structured extractions router."""
    app = FastAPI()

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    mock_extraction_service = AsyncMock()
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[require_project_membership] = lambda: None
    app.dependency_overrides[_get_extraction_service] = lambda: mock_extraction_service

    app.include_router(router)

    # Store mock on app for test access
    app.state._mock_extraction_service = mock_extraction_service
    return app


NOW = datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_list_structured_extractions_success() -> None:
    """GET list returns 200 with a list of structured extractions."""
    app = _create_app()
    mock_service = app.state._mock_extraction_service
    mock_service.get_session_extractions.return_value = StructuredExtractionListResponse(
        items=[
            StructuredExtractionResponse(
                id=UUID("00000000-0000-0000-0000-000000000010"),
                session_id=SESSION_ID,
                episode_id=EPISODE_ID,
                schema_id=UUID("00000000-0000-0000-0000-000000000030"),
                data={"order_total": 42.99, "currency": "USD"},
                created_at=NOW,
            ),
        ],
        total=1,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}/structured-extractions",
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["data"]["order_total"] == 42.99
    assert body["items"][0]["episode_id"] == str(EPISODE_ID)

    mock_service.get_session_extractions.assert_awaited_once_with(
        org_id=ORG_ID,
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
    )


@pytest.mark.asyncio
async def test_list_structured_extractions_empty() -> None:
    """GET list returns 200 with an empty list when no extractions exist."""
    app = _create_app()
    mock_service = app.state._mock_extraction_service
    mock_service.get_session_extractions.return_value = StructuredExtractionListResponse(
        items=[],
        total=0,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}/structured-extractions",
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


@pytest.mark.asyncio
async def test_get_episode_extraction_success() -> None:
    """GET by episode_id returns 200 with the structured extraction."""
    app = _create_app()
    mock_service = app.state._mock_extraction_service
    mock_service.get_episode_extraction.return_value = StructuredExtractionResponse(
        id=UUID("00000000-0000-0000-0000-000000000010"),
        session_id=SESSION_ID,
        episode_id=EPISODE_ID,
        schema_id=None,
        data={"summary": "Customer requested a refund."},
        created_at=NOW,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}/structured-extractions/{EPISODE_ID}",
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["summary"] == "Customer requested a refund."
    assert body["episode_id"] == str(EPISODE_ID)

    mock_service.get_episode_extraction.assert_awaited_once_with(
        org_id=ORG_ID,
        session_id=SESSION_ID,
        episode_id=EPISODE_ID,
        project_id=PROJECT_ID,
    )


@pytest.mark.asyncio
async def test_get_episode_extraction_404() -> None:
    """GET by episode_id returns 404 when no extraction exists."""
    app = _create_app()
    mock_service = app.state._mock_extraction_service
    mock_service.get_episode_extraction.return_value = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}/structured-extractions/{EPISODE_ID}",
        )

    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body
    assert str(EPISODE_ID) in body["detail"]
