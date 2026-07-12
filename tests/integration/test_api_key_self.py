"""Tests for the API key self-service endpoint (GET /v1/api-key/project-id).

The endpoint resolves the project_id associated with the calling API key.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
]


class TestApiKeyProjectId:
    """Verify the project-id resolution endpoint."""

    async def test_resolve_project_id_with_api_key(
        self, auth_client: AsyncClient
    ) -> None:
        """API-key-authenticated request returns the key's project_id."""
        response = await auth_client.get("/v1/api-key/project-id")
        assert response.status_code == 200
        body = response.json()
        assert "project_id" in body
        # project_id should be a valid UUID (the key is project-scoped)
        UUID(body["project_id"])

    async def test_resolve_project_id_without_auth(
        self, async_client: AsyncClient
    ) -> None:
        """Unauthenticated request returns 401."""
        response = await async_client.get("/v1/api-key/project-id")
        assert response.status_code == 401
