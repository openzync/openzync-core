"""Unit tests for session router guard clauses.

Validates that the request parameter extractors
(:func:`dependencies.request.get_current_org_id` and
:func:`dependencies.request.get_project_id`) raise the correct domain
exceptions when ``request.state.org_id`` is missing or
``request.path_params["project_id"]`` is absent, rather than letting
``UUID(None)`` / ``KeyError`` produce an unhandled 500.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest
from starlette.requests import Request

from core.exceptions import AuthenticationError, ValidationError
from dependencies.request import get_current_org_id, get_project_id


class TestOrgIdGuard:
    """``get_current_org_id`` raises ``AuthenticationError`` when org context is missing."""

    @pytest.mark.unit
    async def test_missing_org_id_raises(self) -> None:
        """When ``request.state.org_id`` is ``None``, raise ``AuthenticationError``."""
        request = MagicMock(spec=Request)
        request.state.org_id = None

        with pytest.raises(
            AuthenticationError,
            match="Organization context not set",
        ):
            await get_current_org_id(request)

    @pytest.mark.unit
    async def test_missing_org_id_attr_raises(self) -> None:
        """When ``request.state`` has no ``org_id`` attribute, raise ``AuthenticationError``."""
        request = MagicMock(spec=Request)
        # Use a spec that doesn't have org_id on state
        del request.state.org_id

        with pytest.raises(
            AuthenticationError,
            match="Organization context not set",
        ):
            await get_current_org_id(request)

    @pytest.mark.unit
    async def test_valid_org_id_returns_uuid(self) -> None:
        """A valid org_id string is returned as a UUID."""
        request = MagicMock(spec=Request)
        request.state.org_id = "550e8400-e29b-41d4-a716-446655440000"

        result = await get_current_org_id(request)
        assert result == UUID("550e8400-e29b-41d4-a716-446655440000")


class TestProjectIdGuard:
    """``get_project_id`` raises ``ValidationError`` when project_id is missing."""

    @pytest.mark.unit
    async def test_missing_project_id_raises(self) -> None:
        """When ``project_id`` is missing from path params, raise ``ValidationError``."""
        request = MagicMock(spec=Request)
        request.path_params = {}

        with pytest.raises(
            ValidationError,
            match="Project ID is required in path",
        ):
            await get_project_id(request)

    @pytest.mark.unit
    async def test_empty_project_id_raises(self) -> None:
        """When ``project_id`` is empty string, raise ``ValidationError``."""
        request = MagicMock(spec=Request)
        request.path_params = {"project_id": ""}

        with pytest.raises(
            ValidationError,
            match="Project ID is required in path",
        ):
            await get_project_id(request)

    @pytest.mark.unit
    async def test_invalid_project_id_raises_value_error(self) -> None:
        """When ``project_id`` is not a valid UUID, let ``ValueError`` propagate.

        The router receives the dependency injection error as a 422; the raw
        ``ValueError`` from ``UUID()`` is acceptable here since Pydantic-style
        validation at the dependency layer is an explicit FastAPI pattern.
        """
        request = MagicMock(spec=Request)
        request.path_params = {"project_id": "not-a-uuid"}

        with pytest.raises(ValueError, match="badly formed hexadecimal UUID"):
            await get_project_id(request)

    @pytest.mark.unit
    async def test_valid_project_id_returns_uuid(self) -> None:
        """A valid project_id string is returned as a UUID."""
        request = MagicMock(spec=Request)
        request.path_params = {"project_id": "550e8400-e29b-41d4-a716-446655440000"}

        result = await get_project_id(request)
        assert result == UUID("550e8400-e29b-41d4-a716-446655440000")
