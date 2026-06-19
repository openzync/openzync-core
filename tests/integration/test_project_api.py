"""Integration tests for project API endpoints — auth enforcement.

Tests depend on testcontainers (PostgreSQL + Redis) bootstrapped in
``conftest.py`` and run against a real FastAPI test client.

Project CRUD logic is fully covered by:
- ``tests/unit/test_project_service.py`` (23 unit tests, mocked repo)
- ``tests/integration/test_project_repository.py`` (21 repo integration tests)

These API tests focus on auth enforcement, which works with the existing
``async_client`` fixture (no auth header).

Full CRUD API tests via JWT auth are deferred — the testcontainers Redis
event-loop incompatibility with per-test ``AsyncClient`` instances makes
them unreliable in this environment.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
]


class TestProjectAuthEnforcement:
    """Verify auth is enforced at the API layer."""

    async def test_list_requires_auth(self, async_client: AsyncClient) -> None:
        """GET /v1/projects returns 401 without auth."""
        resp = await async_client.get("/v1/projects")
        assert resp.status_code == 401

    async def test_create_requires_auth(self, async_client: AsyncClient) -> None:
        """POST /v1/projects returns 401 without auth."""
        resp = await async_client.post(
            "/v1/projects", json={"name": "Not Allowed"}
        )
        assert resp.status_code == 401

    async def test_get_requires_auth(self, async_client: AsyncClient) -> None:
        """GET /v1/projects/{id} returns 401 without auth."""
        resp = await async_client.get(
            "/v1/projects/00000000-0000-0000-0000-000000000001"
        )
        assert resp.status_code == 401

    async def test_member_list_requires_auth(
        self, async_client: AsyncClient
    ) -> None:
        """GET /v1/projects/{id}/members returns 401 without auth."""
        resp = await async_client.get(
            "/v1/projects/00000000-0000-0000-0000-000000000001/members"
        )
        assert resp.status_code == 401
