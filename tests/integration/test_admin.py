"""Integration tests for the admin bootstrap endpoint.

Endpoint: ``POST /admin/organizations``

This is the platform operator's org-bootstrap endpoint.  It is gated behind
the platform ``superadmin`` role (``require_superadmin``) — it is **not**
publicly accessible.  Under the current contract it returns only the org ID
+ name — **no** API key is generated and **no** default project is
auto-created (first-use auth flows through ``POST /v1/auth/signup``).

Tests:
    - ``test_create_organization_no_key_no_default_project`` — happy path
    - ``test_create_org_invalid_plan`` — plan validation → 422
    - ``test_create_org_missing_name`` — required field validation → 422
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from core.config import PLATFORM_ORG_ID, get_settings
from repositories.user_repository import UserRepository
from tests.integration.conftest import asgi_transport
from utils.crypto import create_jwt_token


class TestAdminBootstrap:
    """Validate ``POST /admin/organizations`` (superadmin-gated)."""

    # ═════════════════════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════════════════════

    @pytest.fixture
    async def superadmin_client(self, app: pytest.fixture) -> AsyncClient:  # noqa: ARG002
        """Return a superadmin-authenticated HTTP client.

        ``POST /admin/organizations`` is gated by ``require_superadmin`` —
        a JWT session in the platform org whose DB role is ``superadmin``.
        We seed that user directly (test infra) and mint a JWT.
        """
        from sqlalchemy import text

        async with app.state.db_session_factory() as session:
            # Ensure the platform org exists (the superadmin must belong to it).
            await session.execute(
                text(
                    "INSERT INTO organizations (id, name, plan, org_code) "
                    "VALUES (:id, 'SYSTEM', 'free', 'platform') "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": PLATFORM_ORG_ID},
            )
            user = await UserRepository(session).create(
                organization_id=PLATFORM_ORG_ID,
                external_id=f"superadmin_{uuid4().hex[:8]}",
                role="superadmin",
            )
            # Clear the must-change-password gate so the dashboard session
            # is usable (the seeded root is the only user with that flag).
            await session.execute(
                text("UPDATE users SET must_change_password = false WHERE id = :id"),
                {"id": user.id},
            )
            await session.commit()
        user_id = user.id

        jwt_token = create_jwt_token(
            data={
                "sub": str(user_id),
                "org_id": str(PLATFORM_ORG_ID),
                "role": "superadmin",
                "type": "access",
            },
            secret=get_settings().SECRET_KEY,
            expires_delta=timedelta(hours=1),
        )

        transport = asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            client.headers["Authorization"] = f"Bearer {jwt_token}"
            yield client

    # ═════════════════════════════════════════════════════════════════════
    # Happy path
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_organization_no_key_no_default_project(
        self, superadmin_client: AsyncClient, app: pytest.fixture
    ) -> None:
        """A valid request creates an org — with no API key, no default project.

        The response should include:
        - ``organization_id`` — a valid UUID.
        - ``organization_name`` — the provided name.
        - **No** ``api_key`` field — bootstrap keys were removed.

        And the new org must have zero projects until one is created
        explicitly (no auto-created default project).
        """
        response = await superadmin_client.post(
            "/admin/organizations",
            json={"name": "Test Org", "plan": "free"},
        )

        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.text}"
        )
        body = response.json()

        # -- Response shape assertions --
        assert "organization_id" in body, "Missing organization_id in response"
        assert "organization_name" in body, "Missing organization_name in response"

        # -- Type / format assertions --
        # organization_id must be a valid UUID
        UUID(body["organization_id"])

        # -- No bootstrap API key under the new contract --
        assert "api_key" not in body, (
            "Org bootstrap must not return an api_key — got one: "
            f"{body.get('api_key')}"
        )

        # -- No default project auto-created --
        from sqlalchemy import text

        async with app.state.db_session_factory() as session:
            count = await session.execute(
                text(
                    "SELECT count(*) FROM projects WHERE organization_id = :oid"
                ),
                {"oid": body["organization_id"]},
            )
        assert count.scalar() == 0, (
            "Org bootstrap must not auto-create a default project"
        )

    # ═════════════════════════════════════════════════════════════════════
    # Schema validation errors (422)
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_org_invalid_plan(
        self, superadmin_client: AsyncClient
    ) -> None:
        """An invalid ``plan`` value should return 422.

        Accepted values are typically ``free``, ``pro``, ``enterprise``.
        A value like ``invalid`` should fail Pydantic validation.
        """
        response = await superadmin_client.post(
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
        self, superadmin_client: AsyncClient
    ) -> None:
        """Omitting the required ``name`` field should return 422.

        ``name`` is a required field on the request schema — the endpoint
        must reject requests that omit it.
        """
        response = await superadmin_client.post(
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
