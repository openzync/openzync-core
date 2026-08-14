"""Unit tests for the admin bootstrap router.

Tests the ``POST /admin/organizations`` bootstrap endpoint — now
superadmin-gated.  Every request must present a platform-org JWT session
with a DB-verified ``superadmin`` role; anything else is 401/403 before
the handler runs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import PLATFORM_ORG_ID
from dependencies.db import get_db
from routers.admin import _get_admin_org_service, router
from schemas.organizations import CreateOrgResponse

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
SUPERADMIN_USER_ID = UUID("00000000-0000-0000-0000-0000000000bb")


def _create_app(*, authenticated: bool) -> tuple[FastAPI, AsyncMock]:
    """Build a minimal FastAPI app with the admin router.

    ``authenticated=True`` adds middleware presenting a platform-org
    superadmin JWT session (the role lookup itself is patched in each
    test via ``dependencies.auth.get_org_role``).
    """
    app = FastAPI()
    db_mock = AsyncMock(spec=AsyncSession)
    app.state.redis = AsyncMock()

    app.dependency_overrides[get_db] = lambda: db_mock
    app.include_router(router)

    if authenticated:

        @app.middleware("http")
        async def _superadmin_jwt(request: Request, call_next):
            request.state.org_id = str(PLATFORM_ORG_ID)
            request.state.user_id = str(SUPERADMIN_USER_ID)
            request.state.auth_type = "jwt"
            request.state.role = "superadmin"
            request.state.api_key_scopes = []
            return await call_next(request)

    return app, db_mock


def _mock_service(app: FastAPI, mock_response: CreateOrgResponse) -> AsyncMock:
    """Override the org-service dependency with a mock returning the response."""
    mock_service = AsyncMock()
    mock_service.create_organization.return_value = mock_response
    app.dependency_overrides[_get_admin_org_service] = lambda: mock_service
    return mock_service


@pytest.mark.asyncio
async def test_create_organization_success() -> None:
    """POST /admin/organizations returns 201 for a superadmin.

    New contract: no API key is generated at org creation — the response
    carries only the org id and name, and no default project is created.
    """
    created_org_id = uuid4()
    mock_response = CreateOrgResponse(
        organization_id=created_org_id,
        organization_name="Acme Corp",
    )

    app, _ = _create_app(authenticated=True)
    mock_service = _mock_service(app, mock_response)
    transport = ASGITransport(app=app)

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/organizations",
                json={"name": "Acme Corp", "plan": "free"},
            )

    assert resp.status_code == 201
    body = resp.json()
    assert body["organization_name"] == "Acme Corp"
    assert UUID(body["organization_id"]) == created_org_id
    # No API key material in the new contract.
    assert "api_key" not in body
    assert "api_key_prefix" not in body
    assert "api_key_name" not in body

    mock_service.create_organization.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_organization_default_plan() -> None:
    """POST /admin/organizations defaults plan to 'free' when omitted."""
    created_org_id = uuid4()
    mock_response = CreateOrgResponse(
        organization_id=created_org_id,
        organization_name="Acme Corp",
    )

    app, _ = _create_app(authenticated=True)
    _mock_service(app, mock_response)
    transport = ASGITransport(app=app)

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/organizations",
                json={"name": "Acme Corp"},
            )

    assert resp.status_code == 201
    body = resp.json()
    assert body["organization_name"] == "Acme Corp"
    assert "organization_id" in body
    assert "api_key" not in body


@pytest.mark.asyncio
async def test_create_organization_unauthenticated_401() -> None:
    """No auth → 401 (the bootstrap endpoint is no longer public)."""
    app, _ = _create_app(authenticated=False)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/admin/organizations",
            json={"name": "Acme Corp", "plan": "free"},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_organization_non_superadmin_403() -> None:
    """A tenant-org JWT (even admin role) → 403."""
    app = FastAPI()
    app.state.redis = AsyncMock()
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.include_router(router)

    @app.middleware("http")
    async def _tenant_jwt(request: Request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(SUPERADMIN_USER_ID)
        request.state.auth_type = "jwt"
        request.state.role = "admin"
        request.state.api_key_scopes = []
        return await call_next(request)

    transport = ASGITransport(app=app)
    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="admin"),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/organizations",
                json={"name": "Acme Corp", "plan": "free"},
            )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_organization_422_name_missing() -> None:
    """POST /admin/organizations returns 422 when name is missing (superadmin)."""
    app, _ = _create_app(authenticated=True)
    transport = ASGITransport(app=app)

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/organizations", json={})

    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body


@pytest.mark.asyncio
async def test_create_organization_422_invalid_plan() -> None:
    """POST /admin/organizations returns 422 when plan is invalid."""
    app, _ = _create_app(authenticated=True)
    transport = ASGITransport(app=app)

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/organizations",
                json={"name": "Acme Corp", "plan": "invalid_plan"},
            )

    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body


@pytest.mark.asyncio
async def test_create_organization_empty_name_422() -> None:
    """POST /admin/organizations returns 422 when name is empty."""
    app, _ = _create_app(authenticated=True)
    transport = ASGITransport(app=app)

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/organizations",
                json={"name": ""},
            )

    assert resp.status_code == 422
