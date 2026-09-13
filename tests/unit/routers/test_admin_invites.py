"""Unit tests for the admin invite router.

Tests ``POST /v1/admin/users/invite`` and ``DELETE /v1/admin/users/invites/{user_id}``:
- 201 for org admins (invite response, never the raw token).
- 204 for revoke.
- 403 for org members (JWT, non-admin role).
- 401 for unauthenticated requests.
- 409 duplicate email, 404 no-pending-invite, 502 email-send failure.

Observed contract: both endpoints are admin-only (JWT + org ``admin`` role).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.exceptions import (
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    register_exception_handlers,
)
from dependencies.auth import _check_permission as real_check_permission
from dependencies.auth import get_dashboard_user, require_org_id
from dependencies.services import get_invite_service
from routers.admin_invites import router
from schemas.auth import InviteResponse

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
INVITEE_ID = UUID("00000000-0000-0000-0000-000000000003")


@pytest.fixture(autouse=True)
def _stub_permission_gate() -> None:
    """Stub the permission gate for every test in this file.

    The router gates with ``require_permission("members:write")`` — a
    closure created at router import time that cannot be keyed in
    ``dependency_overrides``.  Patching ``dependencies.auth._check_permission``
    (the shared decision function) stubs the gate while keeping the
    ``require_org_id`` chain intact.  The real gate matrix is covered by
    ``test_admin_gate_matrix.py``.
    """
    with patch("dependencies.auth._check_permission", new=AsyncMock()):
        yield


def _make_app() -> tuple[FastAPI, AsyncMock]:
    """Admin-gated app: admin gate dependencies + invite service mocked."""
    app = FastAPI()
    register_exception_handlers(app)
    service_mock = AsyncMock()
    from dependencies.db import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
    app.dependency_overrides[get_dashboard_user] = lambda: str(USER_ID)
    app.dependency_overrides[get_invite_service] = lambda: service_mock
    app.include_router(router)
    return app, service_mock


def _make_member_app() -> FastAPI:
    """App where the JWT user resolves to the member role (real dependency)."""
    app = FastAPI()
    app.state.redis = AsyncMock()

    from dependencies.db import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_invite_service] = lambda: AsyncMock()

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    app.include_router(router)
    return app


# ── POST /v1/admin/users/invite ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_admin_201() -> None:
    """POST invite returns 201 with the pending user (no token) for admins."""
    app, service_mock = _make_app()
    service_mock.invite_user.return_value = InviteResponse(
        id=INVITEE_ID,
        email="alice@acme.com",
        name="Alice Johnson",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/admin/users/invite",
            json={"email": "alice@acme.com", "name": "Alice Johnson"},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body == {
        "id": str(INVITEE_ID),
        "email": "alice@acme.com",
        "name": "Alice Johnson",
    }
    assert "token" not in body
    service_mock.invite_user.assert_awaited_once()
    call_kwargs = service_mock.invite_user.call_args.kwargs
    assert call_kwargs["admin_user_id"] == USER_ID
    assert call_kwargs["org_id"] == ORG_ID


@pytest.mark.asyncio
async def test_invite_member_403() -> None:
    """POST invite returns 403 for a JWT member (role check)."""
    app = _make_member_app()

    with (
        patch(
            "dependencies.auth._check_permission", new=real_check_permission,
        ),
        patch(
            "dependencies.auth.get_org_role", new=AsyncMock(return_value="member"),
        ),
        patch(
            "dependencies.auth.get_effective_permissions",
            new=AsyncMock(return_value=frozenset()),
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/admin/users/invite",
                json={"email": "alice@acme.com", "name": "Alice Johnson"},
            )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_invite_unauthenticated_401() -> None:
    """POST invite returns 401 with no authentication."""
    app = FastAPI()
    app.state.redis = AsyncMock()
    app.include_router(router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/admin/users/invite",
            json={"email": "alice@acme.com", "name": "Alice Johnson"},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_invite_duplicate_email_409() -> None:
    """POST invite returns 409 when the email already has an account."""
    app, service_mock = _make_app()
    service_mock.invite_user.side_effect = ConflictError(
        "An account with this email already exists."
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/admin/users/invite",
            json={"email": "alice@acme.com", "name": "Alice Johnson"},
        )

    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_invite_email_send_failure_502() -> None:
    """POST invite returns 502 when the invite email cannot be sent.

    The pending row is created but the request-scoped session rolls it
    back with the error (get_db contract) — no orphaned invite.
    """
    app, service_mock = _make_app()
    service_mock.invite_user.side_effect = ExternalServiceError(
        "Failed to send email"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/admin/users/invite",
            json={"email": "alice@acme.com", "name": "Alice Johnson"},
        )

    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_invite_invalid_payload_422() -> None:
    """POST invite returns 422 for a blank name or bad email."""
    app, _ = _make_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/admin/users/invite",
            json={"email": "not-an-email", "name": "   "},
        )

    assert resp.status_code == 422


# ── DELETE /v1/admin/users/invites/{user_id} ───────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_admin_204() -> None:
    """DELETE invite returns 204 for a pending invite."""
    app, service_mock = _make_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/v1/admin/users/invites/{INVITEE_ID}")
        content = resp.content  # read inside the client context

    assert resp.status_code == 204
    # Regression guard: a 204 must carry no body and no JSON content-type.
    # Returning bare None would serialize "null" — TestClient won't reproduce
    # the uvicorn send error, so assert the no-body contract directly.
    assert content == b""
    assert "application/json" not in resp.headers.get("content-type", "")
    service_mock.revoke_invite.assert_awaited_once_with(
        org_id=ORG_ID,
        user_id=INVITEE_ID,
    )


@pytest.mark.asyncio
async def test_revoke_member_403() -> None:
    """DELETE invite returns 403 for a JWT member."""
    app = _make_member_app()

    with (
        patch(
            "dependencies.auth._check_permission", new=real_check_permission,
        ),
        patch(
            "dependencies.auth.get_org_role", new=AsyncMock(return_value="member"),
        ),
        patch(
            "dependencies.auth.get_effective_permissions",
            new=AsyncMock(return_value=frozenset()),
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete(f"/v1/admin/users/invites/{INVITEE_ID}")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_revoke_unauthenticated_401() -> None:
    """DELETE invite returns 401 with no authentication."""
    app = FastAPI()
    app.state.redis = AsyncMock()
    app.include_router(router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/v1/admin/users/invites/{INVITEE_ID}")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoke_no_pending_invite_404() -> None:
    """DELETE invite returns 404 when the user has no pending invite."""
    app, service_mock = _make_app()
    service_mock.revoke_invite.side_effect = NotFoundError(
        "No pending invite for this user."
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/v1/admin/users/invites/{INVITEE_ID}")

    assert resp.status_code == 404
