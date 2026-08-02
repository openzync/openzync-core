"""Unit tests for the admin quick actions router.

Tests ``GET /v1/admin/quick-actions`` endpoint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dependencies.auth import get_dashboard_user, require_org_id
from dependencies.services import get_quick_actions_service
from routers.admin_quick_actions import router

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


def _create_app() -> tuple[FastAPI, dict[str, AsyncMock]]:
    """Build a minimal FastAPI app with the quick actions router."""
    app = FastAPI()
    mocks: dict[str, AsyncMock] = {}

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    mocks["quick_actions_service"] = AsyncMock()

    app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
    app.dependency_overrides[get_dashboard_user] = lambda: str(USER_ID)
    app.dependency_overrides[get_quick_actions_service] = lambda: mocks["quick_actions_service"]

    app.include_router(router)
    return app, mocks


@pytest.mark.asyncio
async def test_list_quick_actions_success() -> None:
    """GET /v1/admin/quick-actions returns 200 with action list."""
    app, mocks = _create_app()
    transport = ASGITransport(app=app)

    mocks["quick_actions_service"].get_actions.return_value = [
        {
            "label": "Create your first project",
            "href": "/projects",
            "icon": "folder-kanban",
            "description": "Projects organize sessions, memory, and graph data",
        },
        {
            "label": "Configure LLM Provider",
            "href": "/settings/org-config/llm",
            "icon": "brain-circuit",
            "description": "Set up your LLM provider to start using sessions",
        },
    ]

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/admin/quick-actions")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["actions"]) == 2
    assert body["actions"][0]["label"] == "Create your first project"
    assert body["actions"][0]["icon"] == "folder-kanban"
    assert body["actions"][1]["label"] == "Configure LLM Provider"

    mocks["quick_actions_service"].get_actions.assert_awaited_once_with(ORG_ID)


@pytest.mark.asyncio
async def test_list_quick_actions_empty() -> None:
    """GET /v1/admin/quick-actions returns 200 with empty action list."""
    app, mocks = _create_app()
    transport = ASGITransport(app=app)

    mocks["quick_actions_service"].get_actions.return_value = []

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/admin/quick-actions")

    assert resp.status_code == 200
    body = resp.json()
    assert body["actions"] == []


@pytest.mark.asyncio
async def test_list_quick_actions_single_action() -> None:
    """GET /v1/admin/quick-actions returns 200 with one action (no description)."""
    app, mocks = _create_app()
    transport = ASGITransport(app=app)

    mocks["quick_actions_service"].get_actions.return_value = [
        {
            "label": "View Projects",
            "href": "/projects",
            "icon": "folder-kanban",
            # description is intentionally omitted
        },
    ]

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/admin/quick-actions")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["actions"]) == 1
    assert body["actions"][0]["label"] == "View Projects"
    assert body["actions"][0]["description"] is None


@pytest.mark.asyncio
async def test_quick_actions_requires_auth() -> None:
    """GET /v1/admin/quick-actions returns 401 when unauthenticated."""
    app = FastAPI()
    # Set up db_session_factory so get_db succeeds.
    # require_org_id will raise 401 naturally because no middleware sets org_id.
    app.state.db_session_factory = MagicMock()
    app.include_router(router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/admin/quick-actions")

    assert resp.status_code == 401


def _raise_401():
    from fastapi import HTTPException, status

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
