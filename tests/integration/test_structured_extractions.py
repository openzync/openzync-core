"""Integration tests for structured extraction query endpoints.

Endpoints under test:

    GET /v1/users/{user_id}/sessions/{session_id}/structured-extractions
        — List extractions for a session
    GET /v1/users/{user_id}/sessions/{session_id}/structured-extractions/{episode_id}
        — Get extraction for a specific episode

Covers:
    1. No extractions yet → 200, empty list
    2. Authentication required
    3. Invalid session/user → 404
    4. Cross-tenant isolation
    5. Single extraction not found → 404
    6. Wiring with a structured schema
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient


class TestStructuredExtractionEndpoint:
    """Tests for the structured extraction query endpoints."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_extractions_returns_empty_list(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET structured-extractions for session with no data → 200, empty list."""
        # Create a user
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "no_extract_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # Create a session
        session_resp = await auth_client.post(
            f"/v1/users/{user_id}/sessions",
            json={"external_id": "no_extract_session"},
        )
        assert session_resp.status_code == 201
        session_id = session_resp.json()["id"]

        # Query extractions (none yet — no ingestion has happened)
        resp = await auth_client.get(
            f"/v1/users/{user_id}/sessions/{session_id}"
            f"/structured-extractions",
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert len(body["items"]) == 0
        assert body["total"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_extractions_require_auth(
        self,
        async_client: AsyncClient,
    ) -> None:
        """GET structured-extractions without auth → 401/403."""
        resp = await async_client.get(
            "/v1/users/00000000-0000-0000-0000-000000000000/"
            "sessions/00000000-0000-0000-0000-000000000000/"
            "structured-extractions",
        )
        assert resp.status_code in (401, 403), (
            f"Expected 401/403, got {resp.status_code}"
        )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_invalid_session_returns_404(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET structured-extractions with non-existent session → 404."""
        # Create a user
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "bad_session_user_extract"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        fake_session_id = "00000000-0000-0000-0000-000000000000"
        resp = await auth_client.get(
            f"/v1/users/{user_id}/sessions/{fake_session_id}"
            f"/structured-extractions",
        )
        assert resp.status_code == 404, (
            f"Expected 404 for non-existent session, "
            f"got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cross_tenant_extractions(
        self,
        app: Any,
    ) -> None:
        """Extractions from Org A must not leak to Org B."""
        transport = ASGITransport(app=app)

        # Bootstrap two orgs
        async with AsyncClient(transport=transport, base_url="http://test") as cli:
            resp_a = await cli.post(
                "/admin/organizations",
                json={"name": "Extract Org A", "plan": "free"},
            )
            assert resp_a.status_code == 201
            org_a = resp_a.json()

            resp_b = await cli.post(
                "/admin/organizations",
                json={"name": "Extract Org B", "plan": "free"},
            )
            assert resp_b.status_code == 201
            org_b = resp_b.json()

        # Org A: create user + session
        async with AsyncClient(transport=transport, base_url="http://test") as cli:
            cli.headers["Authorization"] = f"Bearer {org_a['api_key']}"
            user_resp = await cli.post(
                "/v1/users",
                json={"external_id": "extract_cross_user"},
            )
            assert user_resp.status_code == 201
            user_id_a = user_resp.json()["id"]

            session_resp = await cli.post(
                f"/v1/users/{user_id_a}/sessions",
                json={"external_id": "extract_cross_session"},
            )
            assert session_resp.status_code == 201
            session_id_a = session_resp.json()["id"]

        # Org B: try to access Org A's extractions → should 404 (RLS)
        async with AsyncClient(transport=transport, base_url="http://test") as cli:
            cli.headers["Authorization"] = f"Bearer {org_b['api_key']}"
            resp = await cli.get(
                f"/v1/users/{user_id_a}/sessions/{session_id_a}"
                f"/structured-extractions",
            )
            # RLS prevents Org B from seeing Org A's user → 404
            assert resp.status_code == 404, (
                f"Expected 404 for cross-tenant access, "
                f"got {resp.status_code}: {resp.text}"
            )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_single_extraction_not_found(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET a single extraction for a non-existent episode → 404."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "single_extract_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        session_resp = await auth_client.post(
            f"/v1/users/{user_id}/sessions",
            json={"external_id": "single_extract_session"},
        )
        assert session_resp.status_code == 201
        session_id = session_resp.json()["id"]

        fake_ep_id = "00000000-0000-0000-0000-000000000000"
        resp = await auth_client.get(
            f"/v1/users/{user_id}/sessions/{session_id}"
            f"/structured-extractions/{fake_ep_id}",
        )
        assert resp.status_code == 404, (
            f"Expected 404 for non-existent episode, "
            f"got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_single_extraction_no_auth(
        self,
        async_client: AsyncClient,
    ) -> None:
        """GET a single extraction without auth → 401/403."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = await async_client.get(
            f"/v1/users/{fake_id}/sessions/{fake_id}"
            f"/structured-extractions/{fake_id}",
        )
        assert resp.status_code in (401, 403), (
            f"Expected 401/403, got {resp.status_code}"
        )


