"""Unit tests for OrganizationService — business logic."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from schemas.organizations import CreateOrgRequest
from services.organization_service import OrganizationService


@pytest.mark.unit
class TestOrganizationService:
    @pytest.mark.asyncio
    async def test_create_organization_returns_keys(self) -> None:
        """Creating an org returns org_id + api_key."""
        # MagicMock + real async functions for methods that need ``await``.
        # Avoids ``AsyncMock`` altogether — its ``__call__`` creates a
        # coroutine on every invocation, and if *any* code path calls the
        # mock without ``await`` (even transitively through another
        # repository), Python warns about an unawaited coroutine at GC.
        mock_exec_result = MagicMock()
        mock_exec_result.scalar_one_or_none = MagicMock(return_value=None)

        async def mock_execute(*args: Any, **kwargs: Any) -> MagicMock:
            return mock_exec_result

        async def mock_flush(*args: Any, **kwargs: Any) -> None:
            return None

        async def mock_commit(*args: Any, **kwargs: Any) -> None:
            return None

        async def mock_refresh(*args: Any, **kwargs: Any) -> None:
            return None

        mock_db = MagicMock()
        mock_db.add = MagicMock()
        mock_db.execute = mock_execute
        mock_db.flush = mock_flush
        mock_db.commit = mock_commit
        mock_db.refresh = mock_refresh
        # Mock the org model
        mock_org = MagicMock()
        mock_org.id = UUID("00000000-0000-0000-0000-000000000001")
        mock_org.name = "Test Org"

        service = OrganizationService(db=mock_db)
        # Replace the Organization constructor to return our mock
        import services.organization_service as os_mod

        original_org = os_mod.Organization
        os_mod.Organization = lambda *a, **kw: mock_org  # type: ignore[assignment]
        original_apikey = os_mod.ApiKey

        try:
            payload = CreateOrgRequest(name="Test Org")
            result = await service.create_organization(payload=payload)
            assert result.organization_id is not None
            assert result.api_key is not None
            assert result.organization_name == "Test Org"
        finally:
            os_mod.Organization = original_org
            os_mod.ApiKey = original_apikey
