"""Unit tests for dependencies/auth.py — auth/dashboard dependency functions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException, Request

pytestmark = pytest.mark.unit


class TestGetOrgId:
    """get_org_id: optional org_id extraction from request.state."""

    ORG_ID_STR = "00000000-0000-0000-0000-000000000001"

    @pytest.mark.asyncio
    async def test_returns_org_id_when_present(self) -> None:
        """org_id present in request.state → returns it."""
        from dependencies.auth import get_org_id

        request = MagicMock(spec=Request)
        request.state.org_id = self.ORG_ID_STR
        result = await get_org_id(request)
        assert result == self.ORG_ID_STR

    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self) -> None:
        """org_id absent from request.state → returns None."""
        from dependencies.auth import get_org_id

        request = MagicMock(spec=Request)
        request.state.org_id = None
        result = await get_org_id(request)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_state_attr_missing(self) -> None:
        """request.state has no org_id attribute → returns None."""
        from dependencies.auth import get_org_id

        request = MagicMock(spec=Request)
        del request.state.org_id
        result = await get_org_id(request)
        assert result is None


class TestRequireOrgId:
    """require_org_id: mandatory org_id — raises 401 on missing."""

    ORG_ID_STR = "00000000-0000-0000-0000-000000000001"

    @pytest.mark.asyncio
    async def test_returns_org_id_when_authenticated(self) -> None:
        """Valid org_id → returns it unchanged."""
        from dependencies.auth import require_org_id

        result = await require_org_id(self.ORG_ID_STR)
        assert result == self.ORG_ID_STR

    @pytest.mark.asyncio
    async def test_raises_401_when_none(self) -> None:
        """None org_id → raises HTTPException 401."""
        from dependencies.auth import require_org_id

        with pytest.raises(HTTPException) as exc:
            await require_org_id(None)
        assert exc.value.status_code == 401


class TestRequireScope:
    """require_scope: dependency factory checking API key scopes."""

    ORG_ID_STR = "00000000-0000-0000-0000-000000000001"

    @pytest.mark.asyncio
    async def test_jwt_member_level_scope_passes_without_role_check(self) -> None:
        """auth_type=jwt + member-level scope → passes without a role lookup.

        The role gate only applies to ``admin``-prefixed scopes; member-level
        scopes (``read``, ``write``, ``sessions:read``) pass for any
        authenticated JWT user without touching the DB or Redis.
        """
        from dependencies.auth import require_scope

        checker = require_scope("sessions:read")
        request = MagicMock(spec=Request)
        request.state.auth_type = "jwt"
        request.state.user_id = str(
            UUID("00000000-0000-0000-0000-000000000002")
        )

        result = await checker(request, self.ORG_ID_STR, db=MagicMock())
        assert result == self.ORG_ID_STR

    @pytest.mark.asyncio
    async def test_jwt_admin_scope_requires_role_check(self) -> None:
        """auth_type=jwt + admin:write scope → role-checked, denied for members.

        Admin-prefixed scopes on a JWT session are gated on the org ``admin``
        role (DB-verified via ``core.rbac.get_org_role``) — a member raises 403.
        """
        from unittest.mock import AsyncMock, patch

        from dependencies.auth import require_scope

        checker = require_scope("admin:write")
        request = MagicMock(spec=Request)
        request.state.auth_type = "jwt"
        request.state.user_id = str(
            UUID("00000000-0000-0000-0000-000000000002")
        )
        request.app.state.redis = AsyncMock()

        with patch(
            "dependencies.auth.get_org_role", new=AsyncMock(return_value="member")
        ), pytest.raises(HTTPException) as exc:
            await checker(request, self.ORG_ID_STR, db=MagicMock())
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_api_key_with_valid_scope_passes(self) -> None:
        """API key with correct scope → returns org_id."""
        from dependencies.auth import require_scope

        checker = require_scope("sessions:read")
        request = MagicMock(spec=Request)
        request.state.auth_type = "api_key"
        request.state.api_key_scopes = ["sessions:read", "sessions:write"]

        result = await checker(request, self.ORG_ID_STR)
        assert result == self.ORG_ID_STR

    @pytest.mark.asyncio
    async def test_api_key_without_scope_raises_403(self) -> None:
        """API key missing the required scope → raises 403."""
        from dependencies.auth import require_scope

        checker = require_scope("admin:write")
        request = MagicMock(spec=Request)
        request.state.auth_type = "api_key"
        request.state.api_key_scopes = ["sessions:read"]

        with pytest.raises(HTTPException) as exc:
            await checker(request, self.ORG_ID_STR)
        assert exc.value.status_code == 403
        assert "admin:write" in exc.value.detail["detail"]

    @pytest.mark.asyncio
    async def test_api_key_empty_scopes_raises_403(self) -> None:
        """API key with empty scopes → raises 403."""
        from dependencies.auth import require_scope

        checker = require_scope("any:scope")
        request = MagicMock(spec=Request)
        request.state.auth_type = "api_key"
        request.state.api_key_scopes = []

        with pytest.raises(HTTPException) as exc:
            await checker(request, self.ORG_ID_STR)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_auth_type_none_raises_403(self) -> None:
        """auth_type not set → defaults to api_key check → raises 403."""
        from dependencies.auth import require_scope

        checker = require_scope("any:scope")
        request = MagicMock(spec=Request)
        request.state.auth_type = None
        request.state.api_key_scopes = []

        with pytest.raises(HTTPException) as exc:
            await checker(request, self.ORG_ID_STR)
        assert exc.value.status_code == 403


class TestGetDashboardUser:
    """get_dashboard_user: requires JWT auth, returns user_id."""

    ORG_ID_STR = "00000000-0000-0000-0000-000000000001"
    USER_ID_STR = "00000000-0000-0000-0000-000000000002"

    def _jwt_request(self, path: str = "/v1/admin/stats/org", method: str = "GET"):
        """Build a JWT-authenticated request mock.

        ``app.state.redis`` is disabled by default so the must-change
        gate is skipped unless a test opts in explicitly.
        """
        request = MagicMock(spec=Request)
        request.state.auth_type = "jwt"
        request.state.user_id = self.USER_ID_STR
        request.url.path = path
        request.method = method
        request.app.state.redis = None
        return request

    @pytest.mark.asyncio
    async def test_jwt_with_user_id_returns_id(self) -> None:
        """JWT auth with valid user_id → returns user_id string."""
        from dependencies.auth import get_dashboard_user

        request = self._jwt_request()

        result = await get_dashboard_user(request, self.ORG_ID_STR, AsyncMock())
        assert result == self.USER_ID_STR

    @pytest.mark.asyncio
    async def test_api_key_auth_raises_401(self) -> None:
        """API key auth → raises 401 (dashboard requires JWT)."""
        from dependencies.auth import get_dashboard_user

        request = MagicMock(spec=Request)
        request.state.auth_type = "api_key"

        with pytest.raises(HTTPException) as exc:
            await get_dashboard_user(request, self.ORG_ID_STR)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_no_auth_type_raises_401(self) -> None:
        """auth_type missing → raises 401."""
        from dependencies.auth import get_dashboard_user

        request = MagicMock(spec=Request)
        request.state.auth_type = None

        with pytest.raises(HTTPException) as exc:
            await get_dashboard_user(request, self.ORG_ID_STR)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_jwt_missing_user_id_raises_401(self) -> None:
        """JWT auth without user_id → raises 401."""
        from dependencies.auth import get_dashboard_user

        request = MagicMock(spec=Request)
        request.state.auth_type = "jwt"
        request.state.user_id = None

        with pytest.raises(HTTPException) as exc:
            await get_dashboard_user(request, self.ORG_ID_STR)
        assert exc.value.status_code == 401

    # ── Must-change-password gate ────────────────────────────────────────

    def _flag_request(self, path: str, method: str = "GET") -> MagicMock:
        """Build a JWT request with a Redis mock whose flag reads True."""
        request = self._jwt_request(path=path, method=method)
        redis = MagicMock()
        redis.get.return_value = b"1"  # cached must_change_password = True
        request.app.state.redis = redis
        return request

    @pytest.mark.asyncio
    async def test_must_change_password_blocks_normal_route_403(self) -> None:
        """Flag set → 403 on a normal dashboard route."""
        from dependencies.auth import get_dashboard_user

        request = self._flag_request("/v1/admin/stats/org")
        with patch(
            "dependencies.auth.get_must_change_password",
            new=AsyncMock(return_value=True),
        ), pytest.raises(HTTPException) as exc:
            await get_dashboard_user(request, self.ORG_ID_STR, AsyncMock())
        assert exc.value.status_code == 403
        assert "Password Change Required" in exc.value.detail["title"]

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/v1/auth/change-password"),
            ("GET", "/v1/auth/me"),
            ("POST", "/v1/auth/logout"),
            ("POST", "/v1/auth/refresh"),
        ],
    )
    @pytest.mark.asyncio
    async def test_must_change_password_exempt_paths_pass(
        self, method: str, path: str
    ) -> None:
        """Flag set → exempt paths still resolve the user (no 403)."""
        from dependencies.auth import get_dashboard_user

        request = self._flag_request(path, method=method)
        with patch(
            "dependencies.auth.get_must_change_password",
            new=AsyncMock(return_value=True),
        ):
            result = await get_dashboard_user(request, self.ORG_ID_STR, AsyncMock())
        assert result == self.USER_ID_STR

    @pytest.mark.asyncio
    async def test_must_change_password_cleared_passes(self) -> None:
        """Flag cleared → normal routes resolve normally."""
        from dependencies.auth import get_dashboard_user

        request = self._flag_request("/v1/admin/stats/org")
        with patch(
            "dependencies.auth.get_must_change_password",
            new=AsyncMock(return_value=False),
        ):
            result = await get_dashboard_user(request, self.ORG_ID_STR, AsyncMock())
        assert result == self.USER_ID_STR


class TestRequireSuperadmin:
    """require_superadmin: platform org + DB-verified superadmin role."""

    PLATFORM_ORG_ID_STR = "00000000-0000-0000-0000-0000000000aa"
    SUPERADMIN_USER_ID_STR = "00000000-0000-0000-0000-0000000000bb"
    TENANT_ORG_ID_STR = "00000000-0000-0000-0000-000000000001"

    def _superadmin_request(self, org_id: str) -> MagicMock:
        """JWT request in the given org, with Redis configured."""
        request = MagicMock(spec=Request)
        request.state.auth_type = "jwt"
        request.state.user_id = self.SUPERADMIN_USER_ID_STR
        request.url.path = "/admin/system/config"
        request.method = "GET"
        redis = MagicMock()
        request.app.state.redis = redis
        return request

    @pytest.mark.asyncio
    async def test_platform_org_superadmin_role_passes(self) -> None:
        """Platform org + DB-verified superadmin → returns org_id."""
        from dependencies.auth import require_superadmin

        request = self._superadmin_request(self.PLATFORM_ORG_ID_STR)
        with (
            patch(
                "dependencies.auth.get_must_change_password",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "dependencies.auth.get_org_role",
                new=AsyncMock(return_value="superadmin"),
            ),
        ):
            result = await require_superadmin(
                request,
                self.PLATFORM_ORG_ID_STR,
                self.SUPERADMIN_USER_ID_STR,
                AsyncMock(),
            )
        assert result == self.PLATFORM_ORG_ID_STR

    @pytest.mark.asyncio
    async def test_tenant_org_denied_403(self) -> None:
        """A JWT in a tenant org is never a superadmin — 403."""
        from dependencies.auth import require_superadmin

        request = self._superadmin_request(self.TENANT_ORG_ID_STR)
        with (
            patch(
                "dependencies.auth.get_org_role",
                new=AsyncMock(return_value="admin"),
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await require_superadmin(
                request,
                self.TENANT_ORG_ID_STR,
                self.SUPERADMIN_USER_ID_STR,
                AsyncMock(),
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_member_role_in_platform_org_denied_403(self) -> None:
        """Platform org but non-superadmin role → 403 (fail-closed)."""
        from dependencies.auth import require_superadmin

        request = self._superadmin_request(self.PLATFORM_ORG_ID_STR)
        with (
            patch(
                "dependencies.auth.get_org_role",
                new=AsyncMock(return_value="member"),
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await require_superadmin(
                request,
                self.PLATFORM_ORG_ID_STR,
                self.SUPERADMIN_USER_ID_STR,
                AsyncMock(),
            )
        assert exc.value.status_code == 403


class TestGetCurrentUserId:
    """get_current_user_id: returns authenticated user as UUID."""

    ORG_ID_STR = "00000000-0000-0000-0000-000000000001"
    USER_ID_STR = "00000000-0000-0000-0000-000000000002"

    @pytest.mark.asyncio
    async def test_returns_uuid_when_user_id_present(self) -> None:
        """user_id in request.state → returns UUID."""
        from dependencies.auth import get_current_user_id

        request = MagicMock(spec=Request)
        request.state.user_id = self.USER_ID_STR

        result = await get_current_user_id(request, self.ORG_ID_STR)
        assert isinstance(result, UUID)
        assert result == UUID(self.USER_ID_STR)

    @pytest.mark.asyncio
    async def test_raises_401_when_user_id_missing(self) -> None:
        """user_id absent → raises 401."""
        from dependencies.auth import get_current_user_id

        request = MagicMock(spec=Request)
        request.state.user_id = None

        with pytest.raises(HTTPException) as exc:
            await get_current_user_id(request, self.ORG_ID_STR)
        assert exc.value.status_code == 401
