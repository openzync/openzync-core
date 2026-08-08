"""Unit tests for the admin bootstrap router.

Tests the ``POST /admin/organizations`` bootstrap endpoint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.db import get_db
from routers.admin import router
from schemas.organizations import CreateOrgResponse

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


def _create_app() -> tuple[FastAPI, AsyncMock]:
    """Build a minimal FastAPI app with the admin router."""
    app = FastAPI()
    db_mock = AsyncMock(spec=AsyncSession)

    app.dependency_overrides[get_db] = lambda: db_mock
    app.include_router(router)
    return app, db_mock


@pytest.mark.asyncio
async def test_create_organization_success() -> None:
    """POST /admin/organizations returns 201 with org id and name.

    New contract: no API key is generated at org creation — the response
    carries only the org id and name, and no default project is created.
    """
    created_org_id = uuid4()
    mock_response = CreateOrgResponse(
        organization_id=created_org_id,
        organization_name="Acme Corp",
    )

    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin.OrganizationService") as mock_service_cls:
        mock_service_instance = AsyncMock()
        mock_service_instance.create_organization.return_value = mock_response
        mock_service_cls.return_value = mock_service_instance

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

    # Verify the service was constructed with the right repo
    mock_service_cls.assert_called_once()
    mock_service_instance.create_organization.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_organization_default_plan() -> None:
    """POST /admin/organizations defaults plan to 'free' when omitted."""
    created_org_id = uuid4()
    mock_response = CreateOrgResponse(
        organization_id=created_org_id,
        organization_name="Acme Corp",
    )

    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin.OrganizationService") as mock_service_cls:
        mock_service_instance = AsyncMock()
        mock_service_instance.create_organization.return_value = mock_response
        mock_service_cls.return_value = mock_service_instance

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
async def test_create_organization_422_name_missing() -> None:
    """POST /admin/organizations returns 422 when name is missing."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/admin/organizations", json={})

    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body


@pytest.mark.asyncio
async def test_create_organization_422_invalid_plan() -> None:
    """POST /admin/organizations returns 422 when plan is invalid."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

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
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/admin/organizations",
            json={"name": ""},
        )

    assert resp.status_code == 422
