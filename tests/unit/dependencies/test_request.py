"""Unit tests for dependencies/request.py — request-scoped helpers."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi import Request

from core.exceptions import AuthenticationError, ValidationError

pytestmark = pytest.mark.unit


class TestGetCurrentOrgId:
    """get_current_user_id: extracts user_id from request.state as UUID."""

    ORG_ID_STR = "00000000-0000-0000-0000-000000000001"

    @pytest.mark.asyncio
    async def test_returns_uuid_when_user_id_present(self) -> None:
        """user_id in request.state → returns UUID."""
        from dependencies.request import get_current_user_id

        request = MagicMock(spec=Request)
        request.state.user_id = self.ORG_ID_STR

        result = await get_current_user_id(request)
        assert isinstance(result, UUID)
        assert result == UUID(self.ORG_ID_STR)

    @pytest.mark.asyncio
    async def test_raises_authentication_error_when_missing(self) -> None:
        """user_id absent → raises AuthenticationError."""
        from dependencies.request import get_current_user_id

        request = MagicMock(spec=Request)
        request.state.user_id = None

        with pytest.raises(AuthenticationError, match="User context not set"):
            await get_current_user_id(request)

    @pytest.mark.asyncio
    async def test_raises_authentication_error_when_empty(self) -> None:
        """user_id is empty string → raises AuthenticationError."""
        from dependencies.request import get_current_user_id

        request = MagicMock(spec=Request)
        request.state.user_id = ""

        with pytest.raises(AuthenticationError, match="User context not set"):
            await get_current_user_id(request)

    @pytest.mark.asyncio
    async def test_raises_authentication_error_when_state_attr_missing(self) -> None:
        """request.state has no user_id attr → raises AuthenticationError."""
        from dependencies.request import get_current_user_id

        request = MagicMock(spec=Request)
        del request.state.user_id

        with pytest.raises(AuthenticationError, match="User context not set"):
            await get_current_user_id(request)


class TestGetProjectId:
    """get_project_id: extracts project_id from path params as UUID."""

    PROJECT_ID_STR = "00000000-0000-0000-0000-0000000000aa"

    @pytest.mark.asyncio
    async def test_returns_uuid_when_in_path(self) -> None:
        """project_id in path params → returns UUID."""
        from dependencies.request import get_project_id

        request = MagicMock(spec=Request)
        request.path_params = {"project_id": self.PROJECT_ID_STR}

        result = await get_project_id(request)
        assert isinstance(result, UUID)
        assert result == UUID(self.PROJECT_ID_STR)

    @pytest.mark.asyncio
    async def test_raises_validation_error_when_missing(self) -> None:
        """project_id absent from path → raises ValidationError."""
        from dependencies.request import get_project_id

        request = MagicMock(spec=Request)
        request.path_params = {}

        with pytest.raises(ValidationError, match="Project ID is required in path"):
            await get_project_id(request)

    @pytest.mark.asyncio
    async def test_raises_validation_error_when_empty(self) -> None:
        """project_id is empty string → raises ValidationError."""
        from dependencies.request import get_project_id

        request = MagicMock(spec=Request)
        request.path_params = {"project_id": ""}

        with pytest.raises(ValidationError, match="Project ID is required in path"):
            await get_project_id(request)

    @pytest.mark.asyncio
    async def test_raises_value_error_on_invalid_uuid(self) -> None:
        """project_id is not a valid UUID → raises ValueError from UUID()."""
        from dependencies.request import get_project_id

        request = MagicMock(spec=Request)
        request.path_params = {"project_id": "not-a-uuid"}

        with pytest.raises(ValueError):
            await get_project_id(request)
