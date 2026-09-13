"""Integration tests for Session CRUD endpoints.

Endpoints under test (all under ``/v1/projects/{project_id}/sessions``):

    POST   /v1/projects/{project_id}/sessions              — Create a session
    GET    /v1/projects/{project_id}/sessions               — List sessions (pagination)
    GET    /v1/projects/{project_id}/sessions/{session_id}  — Get a single session
    GET    /v1/projects/{project_id}/sessions/{session_id}/messages  — Get messages
    DELETE /v1/projects/{project_id}/sessions/{session_id}  — Soft-delete a session

Auth strategy:
    Each test creates a fresh org + API key via the admin bootstrap endpoint,
    creates a user via the Users API, and then exercises session endpoints.

Test cases (8):
    1.  ``test_create_session``            — 201 + SessionResponse shape
    2.  ``test_create_duplicate_session``   — same external_id per user → 409
    3.  ``test_get_session``               — 200 + SessionResponseWithStats
    4.  ``test_list_sessions``             — cursor pagination
    5.  ``test_get_messages``              — 200 + empty message list
    6.  ``test_delete_session``            — 204 + subsequent GET → 404
    7.  ``test_session_cross_tenant``      — org B cannot access org A's session
    8.  ``test_session_not_found``         — non-existent UUID → 404
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from dependencies.auth import get_current_user_id
from tests.integration.conftest import bootstrap_tenant


class TestSessionCrud:
    """Full CRUD lifecycle for the ``/v1/projects/{project_id}/sessions`` endpoints.

    Each test is fully self-contained:
    1. Bootstrap an org via the admin endpoint.
    2. Create a user via POST /v1/users.
    3. Exercise the session endpoint(s) under test.
    """

    # ═════════════════════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    async def _create_user(
        isolated_app: Any, isolated_org_and_key: dict, external_id: str,
    ) -> str:
        """Create a user via the API (JWT admin session) and return the user ID.

        ``POST /v1/users`` is gated by ``require_permission("members:write")``
        — only an admin JWT (wildcard) can create users, not the project-scoped
        API key.  We drive the request through a fresh JWT-authenticated client.
        """
        from tests.integration.conftest import asgi_transport

        transport = asgi_transport(isolated_app)
        async with AsyncClient(transport=transport, base_url="http://test") as cli:
            cli.headers["Authorization"] = (
                f"Bearer {isolated_org_and_key['jwt']}"
            )
            resp = await cli.post(
                "/v1/users",
                json={"external_id": external_id},
            )
        assert resp.status_code == 201, (
            f"User creation failed: {resp.status_code} {resp.text}"
        )
        return resp.json()["id"]

    @staticmethod
    def _assert_session_response_shape(body: dict) -> None:
        """Validate that ``body`` matches the ``SessionResponse`` schema.

        ``SessionResponse`` fields: ``id``, ``project_id``, ``created_by``,
        ``external_id``, ``metadata``, ``is_active``, ``created_at``,
        ``updated_at``, ``closed_at``.
        """
        assert "id" in body, "Missing 'id'"
        assert "created_by" in body, "Missing 'created_by'"
        assert "external_id" in body, "Missing 'external_id'"
        assert "metadata" in body, "Missing 'metadata'"
        assert "created_at" in body, "Missing 'created_at'"
        assert "updated_at" in body, "Missing 'updated_at'"
        assert "closed_at" in body, "Missing 'closed_at'"

        # Validate UUIDs
        UUID(body["id"])
        UUID(body["created_by"])

        # Defaults
        assert body["is_active"] is True
        assert body["closed_at"] is None
        assert isinstance(body["metadata"], dict)

    # ═════════════════════════════════════════════════════════════════════
    # 1.  Create session — happy path
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_session(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict,
        isolated_project_id: UUID,
    ) -> None:
        """POST /sessions → 201 with a valid SessionResponse.

        The response must include ``created_by`` matching the authenticated
        user (from the fixture), and ``external_id`` matching the request.
        """
        _ = await self._create_user(isolated_app, isolated_org_and_key, "session_creator")
        fixture_user_id = isolated_org_and_key["user_id"]

        response = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={
                "external_id": "session_001",
                "metadata": {"channel": "api", "version": "1.0"},
            },
        )
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.text}"
        )
        body = response.json()

        self._assert_session_response_shape(body)
        assert body["created_by"] == str(fixture_user_id)
        assert body["external_id"] == "session_001"
        assert body["metadata"] == {"channel": "api", "version": "1.0"}

    # ═════════════════════════════════════════════════════════════════════
    # 2.  Duplicate session external_id → 409
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_duplicate_session(
        self, isolated_app: Any, isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict, isolated_project_id: UUID,
    ) -> None:
        """POST /sessions with the same external_id for the same project → 409.

        The ``(project_id, external_id)`` unique constraint must prevent
        duplicate session creation.
        """
        user_id = await self._create_user(isolated_app, isolated_org_and_key, "dup_session_user")  # noqa: F841

        # Create the first session
        resp1 = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "dup_session"},
        )
        assert resp1.status_code == 201, f"First creation failed: {resp1.text}"

        # Attempt to create a second session with the same external_id
        response = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "dup_session"},
        )
        assert response.status_code == 409, (
            f"Expected 409 for duplicate external_id, "
            f"got {response.status_code}: {response.text}"
        )

    # ═════════════════════════════════════════════════════════════════════
    # 3.  Get session → 200
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_get_session(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict,
        isolated_project_id: UUID,
    ) -> None:
        """GET /sessions/{id} → 200 with SessionResponse.

        ``SessionResponse`` includes aggregate statistics: ``message_count``,
        ``fact_count``, ``pending_enrichment_count``, ``observation_count``.
        """
        _ = await self._create_user(isolated_app, isolated_org_and_key, "get_session_user")
        fixture_user_id = isolated_org_and_key["user_id"]

        # Create a session
        created = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "get_session_test"},
        )
        assert created.status_code == 201
        session_id = created.json()["id"]

        # Fetch the session
        response = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{session_id}"
        )
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        body = response.json()

        self._assert_session_response_shape(body)
        assert body["id"] == session_id
        assert body["created_by"] == str(fixture_user_id)
        assert body["external_id"] == "get_session_test"

        # Stats fields (should be zero for a fresh session)
        assert body["message_count"] == 0
        assert body["fact_count"] == 0

    # ═════════════════════════════════════════════════════════════════════
    # 4.  List sessions — pagination
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_list_sessions(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict,
        isolated_project_id: UUID,
    ) -> None:
        """GET /sessions with cursor pagination.

        Create 3 sessions, fetch with limit=2:
        - Page 1: 2 items, ``has_more=True``, ``next_cursor`` is not null.
        - Page 2: 1 item,  ``has_more=False``, ``next_cursor`` is null.
        """
        _ = await self._create_user(isolated_app, isolated_org_and_key, "list_sesh_user")

        # Seed 3 sessions
        for i in range(3):
            resp = await isolated_auth_client.post(
                f"/v1/projects/{isolated_project_id}/sessions",
                json={"external_id": f"list_session_{i}"},
            )
            assert resp.status_code == 201, f"Seed failed at index {i}"

        # Page 1
        page1 = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions?limit=2"
        )
        assert page1.status_code == 200
        body1 = page1.json()

        assert "data" in body1, "Missing 'data'"
        assert "next_cursor" in body1, "Missing 'next_cursor'"
        assert "has_more" in body1, "Missing 'has_more'"
        assert len(body1["data"]) == 2, (
            f"Expected 2 items, got {len(body1['data'])}"
        )
        assert body1["has_more"] is True
        assert body1["next_cursor"] is not None

        # Page 2
        page2 = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions?limit=2&cursor={body1['next_cursor']}"
        )
        assert page2.status_code == 200
        body2 = page2.json()
        assert len(body2["data"]) == 1
        assert body2["has_more"] is False
        assert body2["next_cursor"] is None

    # ═════════════════════════════════════════════════════════════════════
    # 5.  Get messages — empty list for a new session
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_get_messages(
        self, isolated_app: Any, isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict, isolated_project_id: UUID,
    ) -> None:
        """GET /sessions/{id}/messages → 200 with empty ``data`` list.

        A session with no ingested messages should return an empty array,
        not an error.
        """
        user_id = await self._create_user(isolated_app, isolated_org_and_key, "msg_user")  # noqa: F841

        # Create a session
        created = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "no_messages_session"},
        )
        assert created.status_code == 201
        session_id = created.json()["id"]

        # Fetch messages
        response = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{session_id}/messages"
        )
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        body = response.json()

        # Empty message list response shape
        assert "data" in body, "Missing 'data'"
        assert "next_cursor" in body or "has_more" in body, (
            "Missing pagination fields"
        )
        assert body["data"] == [], f"Expected empty list, got {body['data']}"
        assert body.get("has_more") is False
        assert body.get("next_cursor") is None

    # ═════════════════════════════════════════════════════════════════════
    # 6.  Delete session → 204
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_delete_session(
        self, isolated_app: Any, isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict, isolated_project_id: UUID,
    ) -> None:
        """DELETE /sessions/{id} → 204, subsequent GET → 404.

        Verify the soft-delete lifecycle:
        - DELETE returns 204 No Content.
        - Fetching the same session immediately after returns 404.
        """
        user_id = await self._create_user(isolated_app, isolated_org_and_key, "del_sesh_user")  # noqa: F841

        # Create session
        created = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "delete_me"},
        )
        assert created.status_code == 201
        session_id = created.json()["id"]

        # Delete
        delete_resp = await isolated_auth_client.delete(
            f"/v1/projects/{isolated_project_id}/sessions/{session_id}"
        )
        assert delete_resp.status_code == 204, (
            f"Expected 204, got {delete_resp.status_code}: {delete_resp.text}"
        )

        # Verify it's gone
        get_resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{session_id}"
        )
        assert get_resp.status_code == 404, (
            f"Expected 404 after delete, got {get_resp.status_code}: {get_resp.text}"
        )

    # ═════════════════════════════════════════════════════════════════════
    # 7.  Cross-tenant isolation for sessions
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_session_cross_tenant(
        self,
        isolated_app: pytest.fixture,  # noqa: ARG002
    ) -> None:
        """A session created by org A must not be accessible by org B.

        1. Create org A → api_key A → project_id A
        2. Create org B → api_key B
        3. Via org A: create a user, then create a session under project A
        4. Via org B: try to GET the session → 404 (org B cannot see org A's project)
        """
        # ── Bootstrap org A ─────────────────────────────────────────────
        async with AsyncClient(
            transport=ASGITransport(app=isolated_app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            tenant_a = await bootstrap_tenant(isolated_app, cli, "Org A")
            project_id_a = tenant_a["project_id"]

        # ── Set up user for org A ───────────────────────────────────────
        async with AsyncClient(
            transport=ASGITransport(app=isolated_app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            # POST /v1/users is gated by members:write — use the admin JWT.
            cli.headers["Authorization"] = f"Bearer {tenant_a['jwt']}"

            # Create a user so get_current_user_id has something to return
            user_resp = await cli.post("/v1/users", json={"external_id": "org_a_user"})
            assert user_resp.status_code == 201
            user_id_a = UUID(user_resp.json()["id"])
            isolated_app.dependency_overrides[get_current_user_id] = lambda: user_id_a

        # ── Bootstrap org B ─────────────────────────────────────────────
        async with AsyncClient(
            transport=ASGITransport(app=isolated_app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            tenant_b = await bootstrap_tenant(isolated_app, cli, "Org B")

        # ── Set up user for org B ───────────────────────────────────────
        async with AsyncClient(
            transport=ASGITransport(app=isolated_app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {tenant_b['jwt']}"
            user_resp = await cli.post("/v1/users", json={"external_id": "org_b_user"})
            assert user_resp.status_code == 201
            user_id_b = UUID(user_resp.json()["id"])
            isolated_app.dependency_overrides[get_current_user_id] = lambda: user_id_b

        # ── Org A: create session ───────────────────────────────────────
        async with AsyncClient(
            transport=ASGITransport(app=isolated_app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {tenant_a['api_key']}"
            isolated_app.dependency_overrides[get_current_user_id] = lambda: user_id_a

            session_resp = await cli.post(
                f"/v1/projects/{project_id_a}/sessions",
                json={"external_id": "cross_tenant_session"},
            )
            assert session_resp.status_code == 201
            session_id = session_resp.json()["id"]

        # ── Org B: try to access Org A's session → 404 ─────────────────
        async with AsyncClient(
            transport=ASGITransport(app=isolated_app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {tenant_b['api_key']}"
            get_resp = await cli.get(
                f"/v1/projects/{project_id_a}/sessions/{session_id}"
            )

        assert get_resp.status_code in (403, 404), (
            f"Org B should not be able to access Org A's session. "
            f"Got {get_resp.status_code}: {get_resp.text}"
        )

    # ═════════════════════════════════════════════════════════════════════
    # 8.  Session not found → 404
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_session_not_found(
        self, isolated_app: Any, isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict, isolated_project_id: UUID,
    ) -> None:
        """GET /sessions with a non-existent UUID → 404."""
        user_id = await self._create_user(
            isolated_app, isolated_org_and_key, "not_found_user",
        )  # noqa: F841
        fake_session_id = "00000000-0000-0000-0000-000000000000"

        response = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{fake_session_id}"
        )
        assert response.status_code == 404, (
            f"Expected 404 for non-existent session, "
            f"got {response.status_code}: {response.text}"
        )
