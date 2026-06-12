"""Unit tests for OrganizationService — business logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.organizations import CreateOrgRequest
from services.organization_service import OrganizationService


@pytest.mark.unit
class TestOrganizationService:
    @pytest.mark.asyncio
    async def test_create_organization_returns_keys(self) -> None:
        """Creating an org returns org_id + api_key."""
        mock_db = AsyncMock(spec=AsyncSession)
        # Mock flush/refresh to set the org id
        mock_org = MagicMock()
        mock_org.id = UUID("00000000-0000-0000-0000-000000000001")
        mock_org.name = "Test Org"
        mock_db.refresh.return_value = None

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
