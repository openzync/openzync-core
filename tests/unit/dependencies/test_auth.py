"""Unit tests for dependencies/auth.py — auth/dashboard dependency functions."""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from unittest.mock import MagicMock

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
    async def test_jwt_auth_grants_all_scopes(self) -> None:
        """auth_type=jwt → scope check passes regardless of scope list."""
        from dependencies.auth import require_scope

        checker = require_scope("admin:write")
        request = MagicMock(spec=Request)
        request.state.auth_type = "jwt"

        result = await checker(request, self.ORG_ID_STR)
        assert result == self.ORG_ID_STR

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

    @pytest.mark.asyncio
    async def test_jwt_with_user_id_returns_id(self) -> None:
        """JWT auth with valid user_id → returns user_id string."""
        from dependencies.auth import get_dashboard_user

        request = MagicMock(spec=Request)
        request.state.auth_type = "jwt"
        request.state.user_id = self.USER_ID_STR

        result = await get_dashboard_user(request, self.ORG_ID_STR)
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
