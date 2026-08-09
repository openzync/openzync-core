"""Unit tests for the admin org-code router.

Tests ``GET /admin/org/org-code`` and ``POST /admin/org/org-code/regenerate``:
- 200 for org admins (returns the current / new join code).
- 403 for org members (JWT, non-admin role).
- 401 for unauthenticated requests.

Observed contract: both endpoints are admin-only (JWT + org ``admin`` role);
regeneration immediately invalidates the previous code.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.exceptions import NotFoundError, register_exception_handlers
from dependencies.auth import require_org_admin
from routers.admin_org_code import _get_org_service, router

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
OLD_CODE = "K7M2Q9X4"
NEW_CODE = "ZZZ2Q9X4"


def _make_app() -> tuple[FastAPI, AsyncMock]:
    """Admin-gated app: ``require_org_admin`` resolved as an org admin.

    The router now delegates to ``OrganizationService`` (N5 layering) — the
    service dependency is overridden with a mock.
    """
    app = FastAPI()
    register_exception_handlers(app)
    service_mock = AsyncMock()
    app.dependency_overrides[require_org_admin] = lambda: str(ORG_ID)
    app.dependency_overrides[_get_org_service] = lambda: service_mock
    app.include_router(router)
    return app, service_mock


def _make_member_app() -> FastAPI:
    """App where the JWT user resolves to the member role (real dependency).

    ``require_org_admin`` runs its full chain — JWT state via middleware,
    Redis on app state, and the role lookup patched to ``"member"``.
    """
    app = FastAPI()
    app.state.redis = AsyncMock()

    from dependencies.db import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    app.include_router(router)
    return app


# ── GET /admin/org/org-code ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_org_code_admin_200() -> None:
    """GET /admin/org/org-code returns 200 with the current code for admins."""
    app, service_mock = _make_app()
    service_mock.get_org_code.return_value = OLD_CODE

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/org/org-code")

    assert resp.status_code == 200
    assert resp.json() == {"org_code": OLD_CODE}
    service_mock.get_org_code.assert_awaited_once_with(ORG_ID)


@pytest.mark.asyncio
async def test_get_org_code_member_403() -> None:
    """GET /admin/org/org-code returns 403 for a JWT member (role check)."""
    app = _make_member_app()
    from dependencies.auth import _ensure_org_admin  # noqa: F401  (module import)

    # Patch the role lookup at the dependency layer — the member role denies.
    with patch(
        "dependencies.auth.get_org_role", new=AsyncMock(return_value="member"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/org/org-code")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_org_code_unauthenticated_401() -> None:
    """GET /admin/org/org-code returns 401 with no authentication."""
    app = FastAPI()
    app.state.redis = AsyncMock()
    app.include_router(router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/org/org-code")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_org_code_404_org_missing() -> None:
    """GET /admin/org/org-code returns 404 when the org no longer exists.

    The service raises NotFoundError; the global exception handler maps it
    to 404 (registered in ``_make_app``).
    """
    app, service_mock = _make_app()
    service_mock.get_org_code.side_effect = NotFoundError(
        message=f"Organization {ORG_ID} not found.",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/org/org-code")

    assert resp.status_code == 404


# ── POST /admin/org/org-code/regenerate ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_regenerate_org_code_admin_200_different_code() -> None:
    """POST regenerate returns 200 with a NEW code (old code rotated out).

    Code generation now lives in the service layer (N5) — the mocked
    service returns the fresh code.
    """
    app, service_mock = _make_app()
    service_mock.regenerate_org_code.return_value = NEW_CODE

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/admin/org/org-code/regenerate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["org_code"] == NEW_CODE
    assert body["org_code"] != OLD_CODE  # rotation: old code is invalid now
    service_mock.regenerate_org_code.assert_awaited_once_with(ORG_ID)


@pytest.mark.asyncio
async def test_regenerate_org_code_member_403() -> None:
    """POST regenerate returns 403 for a JWT member."""
    app = _make_member_app()

    with patch(
        "dependencies.auth.get_org_role", new=AsyncMock(return_value="member"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/org/org-code/regenerate")

    assert resp.status_code == 403
