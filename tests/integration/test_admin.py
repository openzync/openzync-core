"""Integration tests for the admin bootstrap endpoint.

Endpoint: ``POST /admin/organizations``

This is the bootstrap endpoint that creates a new organization and returns
a live API key.  It **must** be publicly accessible (no auth required) since
callers do not yet have an API key when they first call it.

Tests:
    - ``test_create_organization_returns_key`` — happy path
    - ``test_create_org_invalid_plan`` — plan validation → 422
    - ``test_create_org_missing_name`` — required field validation → 422
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient

from tests.integration.conftest import asgi_transport


class TestAdminBootstrap:
    """Validate ``POST /admin/organizations``."""

    # ═════════════════════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════════════════════

    @pytest.fixture
    async def anon_client(self, app: pytest.fixture) -> AsyncClient:  # noqa: ARG002
        """Return an unauthenticated HTTP client."""
        transport = asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    # ═════════════════════════════════════════════════════════════════════
    # Happy path
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_organization_returns_key(
        self, anon_client: AsyncClient
    ) -> None:
        """A valid request creates an org and returns an API key.

        The response should include:
        - ``organization_id`` — a valid UUID.
        - ``api_key`` — a string starting with ``oz_live_``.
        """
        response = await anon_client.post(
            "/admin/organizations",
            json={"name": "Test Org", "plan": "free"},
        )

        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.text}"
        )
        body = response.json()

        # -- Response shape assertions --
        assert "organization_id" in body, "Missing organization_id in response"
        assert "api_key" in body, "Missing api_key in response"

        # -- Type / format assertions --
        # organization_id must be a valid UUID
        UUID(body["organization_id"])

        # api_key must start with the live prefix
        assert body["api_key"].startswith("oz_live_"), (
            f"api_key should start with 'oz_live_', got: {body['api_key'][:10]}..."
        )

    # ═════════════════════════════════════════════════════════════════════
    # Schema validation errors (422)
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_org_invalid_plan(
        self, anon_client: AsyncClient
    ) -> None:
        """An invalid ``plan`` value should return 422.

        Accepted values are typically ``free``, ``pro``, ``enterprise``.
        A value like ``invalid`` should fail Pydantic validation.
        """
        response = await anon_client.post(
            "/admin/organizations",
            json={"name": "Test Org", "plan": "invalid"},
        )

        assert response.status_code == 422, (
            f"Expected 422 for invalid plan, got {response.status_code}: {response.text}"
        )

        # -- Should be a Pydantic-style validation error --
        body = response.json()
        # FastAPI 422 from Pydantic includes "detail" with field-level errors
        assert "detail" in body, "Expected Pydantic validation detail in response"

        # Verify the error mentions the plan field
        detail_str = str(body["detail"]).lower()
        assert "plan" in detail_str, (
            f"Validation error should reference 'plan' field: {detail_str}"
        )

    @pytest.mark.asyncio
    async def test_create_org_missing_name(
        self, anon_client: AsyncClient
    ) -> None:
        """Omitting the required ``name`` field should return 422.

        ``name`` is a required field on the request schema — the endpoint
        must reject requests that omit it.
        """
        response = await anon_client.post(
            "/admin/organizations",
            json={"plan": "free"},  # missing "name"
        )

        assert response.status_code == 422, (
            f"Expected 422 for missing name, got {response.status_code}: {response.text}"
        )

        body = response.json()
        assert "detail" in body, "Expected Pydantic validation detail in response"

        detail_str = str(body["detail"]).lower()
        assert "name" in detail_str, (
            f"Validation error should reference 'name' field: {detail_str}"
        )
