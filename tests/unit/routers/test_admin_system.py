"""Unit tests for routers/admin_system — the members listing endpoint.

The admin gate (require_admin) is exercised through the REAL
dependency chain with a platform-org JWT harness and a patched role
lookup; the repository layer is mocked at the router boundary.

Observed contract:
- GET /admin/system/orgs/{user_id}/members → paginated members of the
  target org (id, email, name, role, is_active), soft-deleted excluded
  by the repo.
- Missing org → 404 (verified before any member query).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from core.config import PLATFORM_ORG_ID
from dependencies.db import get_db, get_db_admin
from dependencies.services import get_admin_user_service
from routers.admin_system import router

ADMIN_USER_ID = UUID("00000000-0000-0000-0000-0000000000bb")
ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
MEMBER_A = UUID("00000000-0000-0000-0000-000000000002")
MEMBER_B = UUID("00000000-0000-0000-0000-000000000003")


def _make_app() -> FastAPI:
    """Build the app with the real admin gate + a bypass-session mock."""
    app = FastAPI()
    app.state.redis = AsyncMock()
    # The bypass session is a plain AsyncMock — the repository classes are
    # patched per-test so no real query runs against it.
    app.dependency_overrides[get_db_admin] = lambda: AsyncMock()
    # require_admin's role lookup needs a session too.
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.include_router(router)

    @app.middleware("http")
    async def _superadmin_jwt(request: Request, call_next):
        request.state.user_id = str(PLATFORM_ORG_ID)
        request.state.user_id = str(ADMIN_USER_ID)
        request.state.auth_type = "jwt"
        request.state.role = "admin"
        request.state.api_key_scopes = []
        return await call_next(request)

    return app


def _make_user(user_id: UUID, *, email: str | None, name: str | None) -> MagicMock:
    """Build a MagicMock mimicking a User ORM row."""
    user = MagicMock()
    user.id = user_id
    user.email = email
    user.external_id = f"ext-{user_id.hex[:6]}"
    user.name = name
    user.role = "member"
    user.is_active = True
    return user


# ── platform system config (GET/PATCH /admin/system/config) ─────────────


# ── list all orgs ────────────────────────────────────────────────────────


# ── org config (cross-org) ───────────────────────────────────────────────


# ── approve / reject ─────────────────────────────────────────────────────


# ── member role PATCH ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_member_role_200_persists() -> None:
    """PATCH role → 200; the demotion is persisted via UserService."""
    app = _make_app()
    service = AsyncMock()
    service.update_member_role.return_value = MagicMock(
        id=MEMBER_B,
        role="member",
    )
    service.update_member_role.return_value.id = MEMBER_B
    app.dependency_overrides[get_admin_user_service] = lambda: service

    with patch(
        "dependencies.auth.get_user_role",
        new=AsyncMock(return_value="admin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                f"/admin/system/users/{MEMBER_B}/role",
                json={"role": "member"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(MEMBER_B)
    assert body["user_id"] == str(MEMBER_B)
    assert body["role"] == "member"

    service.update_member_role.assert_awaited_once_with(
        user_id=MEMBER_B,
        role="member",
    )


@pytest.mark.asyncio
async def test_update_member_role_invalid_role_422() -> None:
    """Role outside {admin, member} → 422 (request schema rejects)."""
    app = _make_app()

    with patch(
        "dependencies.auth.get_user_role",
        new=AsyncMock(return_value="admin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                f"/admin/system/users/{MEMBER_B}/role",
                json={"role": "owner"},
            )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_member_role_missing_user_404() -> None:
    """User not in the org → 404, nothing persisted."""
    from core.exceptions import NotFoundError

    app = _make_app()
    service = AsyncMock()
    service.update_member_role.side_effect = NotFoundError(
        f"User {MEMBER_B} not found."
    )
    app.dependency_overrides[get_admin_user_service] = lambda: service

    with patch(
        "dependencies.auth.get_user_role",
        new=AsyncMock(return_value="admin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                f"/admin/system/users/{MEMBER_B}/role",
                json={"role": "member"},
            )

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]
    service.update_member_role.assert_awaited_once_with(
        user_id=MEMBER_B,
        role="member",
    )
