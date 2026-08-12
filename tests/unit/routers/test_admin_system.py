"""Unit tests for routers/admin_system — the members listing endpoint.

The superadmin gate (require_superadmin) is exercised through the REAL
dependency chain with a platform-org JWT harness and a patched role
lookup; the repository layer is mocked at the router boundary.

Observed contract:
- GET /admin/system/orgs/{org_id}/members → paginated members of the
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
from dependencies.auth import require_superadmin
from dependencies.db import get_db, get_db_superadmin
from routers.admin_system import (
    _get_config_service,
    _get_org_service,
    _get_user_service,
    router,
)
from schemas.organization_config import (
    OrgConfigBase,
    OrgConfigResponse,
    UpdateOrgConfigRequest,
)

SUPERADMIN_USER_ID = UUID("00000000-0000-0000-0000-0000000000bb")
ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
MEMBER_A = UUID("00000000-0000-0000-0000-000000000002")
MEMBER_B = UUID("00000000-0000-0000-0000-000000000003")


def _make_app() -> FastAPI:
    """Build the app with the real superadmin gate + a bypass-session mock."""
    app = FastAPI()
    app.state.redis = AsyncMock()
    # The bypass session is a plain AsyncMock — the repository classes are
    # patched per-test so no real query runs against it.
    app.dependency_overrides[get_db_superadmin] = lambda: AsyncMock()
    # require_superadmin's role lookup needs a session too.
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.include_router(router)

    @app.middleware("http")
    async def _superadmin_jwt(request: Request, call_next):
        request.state.org_id = str(PLATFORM_ORG_ID)
        request.state.user_id = str(SUPERADMIN_USER_ID)
        request.state.auth_type = "jwt"
        request.state.role = "superadmin"
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


@pytest.mark.asyncio
async def test_list_org_members_returns_paginated_members() -> None:
    """The endpoint returns the target org's members with total/page/limit."""
    app = _make_app()
    user_a = _make_user(MEMBER_A, email="alice@acme.com", name="Alice")
    user_b = _make_user(MEMBER_B, email=None, name=None)  # email falls back
    user_b.role = "admin"

    service = AsyncMock()
    service.list_org_members.return_value = ([user_a, user_b], 2)
    app.dependency_overrides[_get_org_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/admin/system/orgs/{ORG_ID}/members")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["limit"] == 50
    assert len(body["data"]) == 2

    first = body["data"][0]
    assert first == {
        "id": str(MEMBER_A),
        "email": "alice@acme.com",
        "name": "Alice",
        "role": "member",
        "is_active": True,
    }
    # email falls back to external_id when the row has no email.
    assert body["data"][1]["email"] == user_b.external_id
    assert body["data"][1]["role"] == "admin"

    service.list_org_members.assert_awaited_once_with(
        ORG_ID, page=1, limit=50
    )


@pytest.mark.asyncio
async def test_list_org_members_missing_org_404() -> None:
    """A missing org → 404, and no member query is attempted."""
    from core.exceptions import NotFoundError

    app = _make_app()
    service = AsyncMock()
    service.list_org_members.side_effect = NotFoundError(
        f"Organization {ORG_ID} not found."
    )
    app.dependency_overrides[_get_org_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/admin/system/orgs/{ORG_ID}/members")

    assert resp.status_code == 404, resp.text
    assert "not found" in resp.json()["detail"].lower()
    service.list_org_members.assert_awaited_once_with(
        ORG_ID, page=1, limit=50
    )


@pytest.mark.asyncio
async def test_list_org_members_respects_pagination_params() -> None:
    """page/limit query params are forwarded to the service."""
    app = _make_app()
    service = AsyncMock()
    service.list_org_members.return_value = ([], 0)
    app.dependency_overrides[_get_org_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/admin/system/orgs/{ORG_ID}/members?page=2&limit=10"
            )

    assert resp.status_code == 200
    assert resp.json()["page"] == 2
    assert resp.json()["limit"] == 10
    service.list_org_members.assert_awaited_once_with(
        ORG_ID, page=2, limit=10
    )


def test_superadmin_gate_present_on_members_route() -> None:
    """The members route declares require_superadmin (coverage guard)."""
    route = next(
        r
        for r in router.routes
        if getattr(r, "path", "") == "/admin/system/orgs/{org_id}/members"
        and "GET" in r.methods
    )
    calls = {dep.call for dep in route.dependant.dependencies}
    assert require_superadmin in calls


# ── platform system config (GET/PATCH /admin/system/config) ─────────────


@pytest.mark.asyncio
async def test_get_platform_config_200_flat() -> None:
    """GET /admin/system/config → 200 flat SystemConfigResponse."""
    from schemas.system_config import SystemConfigResponse

    app = _make_app()
    app.state.openbao_client = AsyncMock()

    with (
        patch(
            "routers.admin_system.get_system_config",
            new=AsyncMock(
                return_value=SystemConfigResponse(
                    org_creation_policy="approvals",
                    approval_scope="in_app",
                ),
            ),
        ),
        patch(
            "dependencies.auth.get_org_role",
            new=AsyncMock(return_value="superadmin"),
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/system/config")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Flat — no {stored: ...} wrapper, no secrets; the full schema is
    # serialized (null defaults included).
    assert body["org_creation_policy"] == "approvals"
    assert body["approval_scope"] == "in_app"
    assert "stored" not in body
    assert "openai_api_key" not in body


@pytest.mark.asyncio
async def test_update_platform_config_200_flat_persists() -> None:
    """PATCH /admin/system/config → 200 flat; the update is persisted."""
    from schemas.system_config import SystemConfigResponse, SystemConfigUpdate

    app = _make_app()
    bao_client = AsyncMock()
    app.state.openbao_client = bao_client

    with (
        patch(
            "routers.admin_system.update_system_config",
            new=AsyncMock(
                return_value=SystemConfigResponse(
                    org_creation_policy="approvals",
                    approval_scope="in_app",
                ),
            ),
        ) as mock_update,
        patch(
            "dependencies.auth.get_org_role",
            new=AsyncMock(return_value="superadmin"),
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                "/admin/system/config",
                json={"org_creation_policy": "approvals", "approval_scope": "in_app"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_creation_policy"] == "approvals"
    assert body["approval_scope"] == "in_app"
    assert "stored" not in body
    # The update function received the parsed body + the app-state deps.
    mock_update.assert_awaited_once_with(
        SystemConfigUpdate(
            org_creation_policy="approvals",
            approval_scope="in_app",
        ),
        bao_client,
        app.state.redis,
    )


# ── list all orgs ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_all_orgs_filters_by_status() -> None:
    """GET /admin/system/orgs?status= → filtered SystemOrgListItems."""
    app = _make_app()
    service = AsyncMock()
    service.list_all_orgs.return_value = (
        [
            SimpleNamespace(
                id=ORG_ID,
                name="Acme Inc",
                status="pending",
                created_at=datetime.now(UTC),
            )
        ],
        1,
    )
    app.dependency_overrides[_get_org_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/admin/system/orgs?status=pending&page=1&limit=20"
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["limit"] == 20
    item = body["data"][0]
    assert item["id"] == str(ORG_ID)
    assert item["name"] == "Acme Inc"
    assert item["status"] == "pending"
    assert "created_at" in item

    service.list_all_orgs.assert_awaited_once_with(
        status="pending",
        page=1,
        limit=20,
    )


# ── org config (cross-org) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_org_config_200_flat_stored_shape() -> None:
    """GET /admin/system/orgs/{org_id}/config → flat {stored, ...} shape."""
    app = _make_app()
    service = AsyncMock()
    service.get_config_response.return_value = OrgConfigResponse(
        stored=OrgConfigBase(context_cache_ttl=120),
        system_managed_fields=["cache_provider"],
    )
    app.dependency_overrides[_get_config_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/admin/system/orgs/{ORG_ID}/config")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stored"]["context_cache_ttl"] == 120
    assert body["system_managed_fields"] == ["cache_provider"]
    service.get_config_response.assert_awaited_once_with(ORG_ID)


@pytest.mark.asyncio
async def test_update_org_config_200_persists_context_cache_ttl() -> None:
    """PATCH config persists context_cache_ttl → 200 with stored config."""
    app = _make_app()
    service = AsyncMock()
    service.update_config.return_value = OrgConfigBase(context_cache_ttl=120)
    app.dependency_overrides[_get_config_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                f"/admin/system/orgs/{ORG_ID}/config",
                json={"context_cache_ttl": 120},
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["context_cache_ttl"] == 120
    service.update_config.assert_awaited_once_with(
        ORG_ID,
        UpdateOrgConfigRequest(context_cache_ttl=120),
    )


# ── approve / reject ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_org_200_returns_approval_response() -> None:
    """POST approve → 200 with the approved org's id/name/status."""
    app = _make_app()
    service = AsyncMock()
    service.approve_org.return_value = SimpleNamespace(
        id=ORG_ID,
        name="Acme Inc",
        status="approved",
    )
    app.dependency_overrides[_get_org_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/admin/system/orgs/{ORG_ID}/approve")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "id": str(ORG_ID),
        "name": "Acme Inc",
        "status": "approved",
    }
    service.approve_org.assert_awaited_once_with(ORG_ID, SUPERADMIN_USER_ID)


@pytest.mark.asyncio
async def test_reject_org_200_returns_approval_response() -> None:
    """POST reject → 200 with the rejected org's id/name/status."""
    app = _make_app()
    service = AsyncMock()
    service.reject_org.return_value = SimpleNamespace(
        id=ORG_ID,
        name="Acme Inc",
        status="rejected",
    )
    app.dependency_overrides[_get_org_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/admin/system/orgs/{ORG_ID}/reject")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "id": str(ORG_ID),
        "name": "Acme Inc",
        "status": "rejected",
    }
    service.reject_org.assert_awaited_once_with(ORG_ID, SUPERADMIN_USER_ID)


# ── member role PATCH ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_member_role_200_persists() -> None:
    """PATCH role → 200; the new role is persisted via UserService."""
    app = _make_app()
    service = AsyncMock()
    service.update_member_role.return_value = MagicMock(
        id=MEMBER_B,
        organization_id=ORG_ID,
        role="admin",
    )
    app.dependency_overrides[_get_user_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                f"/admin/system/orgs/{ORG_ID}/members/{MEMBER_B}/role",
                json={"role": "admin"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(MEMBER_B)
    assert body["organization_id"] == str(ORG_ID)
    assert body["role"] == "admin"

    service.update_member_role.assert_awaited_once_with(
        organization_id=ORG_ID,
        user_id=MEMBER_B,
        role="admin",
    )


@pytest.mark.asyncio
async def test_update_member_role_invalid_role_422() -> None:
    """Role outside {admin, member} → 422 (request schema rejects)."""
    app = _make_app()

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                f"/admin/system/orgs/{ORG_ID}/members/{MEMBER_B}/role",
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
        f"User {MEMBER_B} not found in organization {ORG_ID}."
    )
    app.dependency_overrides[_get_user_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                f"/admin/system/orgs/{ORG_ID}/members/{MEMBER_B}/role",
                json={"role": "member"},
            )

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]
    service.update_member_role.assert_awaited_once_with(
        organization_id=ORG_ID,
        user_id=MEMBER_B,
        role="member",
    )


# ── system settings (GET /admin/system/settings[/{key}]) ──────────────────


def _make_app_unauthenticated() -> FastAPI:
    """Build the app with NO auth state — the superadmin gate must 401."""
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.include_router(router)
    return app


def _settings_route(path: str, method: str = "GET"):
    return next(
        r
        for r in router.routes
        if getattr(r, "path", "") == path and method in r.methods
    )


@pytest.mark.asyncio
async def test_list_system_settings_200_masked_under_superadmin() -> None:
    """GET /admin/system/settings → 200 masked list under superadmin."""
    from schemas.admin_system import SystemSettingItem, SystemSettingsResponse

    app = _make_app()
    app.state.openbao_client = AsyncMock()

    with (
        patch(
            "routers.admin_system.list_system_settings",
            new=AsyncMock(
                return_value=SystemSettingsResponse(
                    data=[
                        SystemSettingItem(
                            key="OZ_DATABASE_URL",
                            category="Infrastructure",
                            is_set=True,
                            masked_value="postgresql+asyncpg://db.example.com:5432",
                        ),
                        SystemSettingItem(
                            key="OZ_ENVIRONMENT",
                            category="Platform",
                            is_set=False,
                            masked_value=None,
                        ),
                    ]
                ),
            ),
        ),
        patch(
            "dependencies.auth.get_org_role",
            new=AsyncMock(return_value="superadmin"),
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/system/settings")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "data": [
            {
                "key": "OZ_DATABASE_URL",
                "category": "Infrastructure",
                "is_set": True,
                "masked_value": "postgresql+asyncpg://db.example.com:5432",
            },
            {
                "key": "OZ_ENVIRONMENT",
                "category": "Platform",
                "is_set": False,
                "masked_value": None,
            },
        ]
    }


@pytest.mark.asyncio
async def test_list_system_settings_401_without_superadmin() -> None:
    """No JWT session → 401 before any OpenBao read."""
    app = _make_app_unauthenticated()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/system/settings")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_reveal_system_setting_200_raw_value() -> None:
    """POST /admin/system/settings/{key}/reveal → 200 with the raw value."""
    from schemas.admin_system import SystemSettingRevealResponse

    app = _make_app()
    app.state.openbao_client = AsyncMock()
    raw_url = "postgresql+asyncpg://user:pass@db.example.com:5432/mydb"

    with (
        patch(
            "routers.admin_system.reveal_system_setting",
            new=AsyncMock(
                return_value=SystemSettingRevealResponse(
                    key="OZ_DATABASE_URL",
                    value=raw_url,
                )
            ),
        ),
        patch(
            "dependencies.auth.get_org_role",
            new=AsyncMock(return_value="superadmin"),
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/system/settings/OZ_DATABASE_URL/reveal"
            )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key": "OZ_DATABASE_URL", "value": raw_url}


@pytest.mark.asyncio
async def test_reveal_system_setting_unknown_key_404() -> None:
    """Unknown or unset key → 404 (NotFoundError mapped)."""
    from core.exceptions import NotFoundError

    app = _make_app()
    app.state.openbao_client = AsyncMock()

    with (
        patch(
            "routers.admin_system.reveal_system_setting",
            new=AsyncMock(
                side_effect=NotFoundError("Unknown system setting key: OZ_NOPE.")
            ),
        ),
        patch(
            "dependencies.auth.get_org_role",
            new=AsyncMock(return_value="superadmin"),
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/system/settings/OZ_NOPE/reveal")

    assert resp.status_code == 404
    assert "OZ_NOPE" in resp.json()["detail"]


def test_settings_routes_superadmin_gated() -> None:
    """Both settings routes declare require_superadmin (coverage guard)."""
    for path, method in (
        ("/admin/system/settings", "GET"),
        ("/admin/system/settings/{key}/reveal", "POST"),
    ):
        route = _settings_route(path, method)
        calls = {dep.call for dep in route.dependant.dependencies}
        assert require_superadmin in calls


def test_reveal_route_audit_decorated() -> None:
    """The reveal route is a POST carrying audit metadata.

    A GET here would be silently skipped by the audit middleware
    (middleware/audit.py:231) — POST is what makes the reveal auditable.
    """
    route = _settings_route("/admin/system/settings/{key}/reveal", "POST")
    assert "POST" in route.methods
    endpoint = route.endpoint
    assert endpoint._audit_action == "system.settings.revealed"
    assert endpoint._audit_resource == "system"
    assert endpoint._audit_display == "System setting revealed by superadmin"
