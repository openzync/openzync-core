"""Integration tests for Session CRUD endpoints.

Endpoints under test (all under ``/v1/users/{user_id}/sessions``):

    POST   /v1/users/{user_id}/sessions              — Create a session
    GET    /v1/users/{user_id}/sessions               — List sessions (pagination)
    GET    /v1/users/{user_id}/sessions/{session_id}  — Get a single session
    GET    /v1/users/{user_id}/sessions/{session_id}/messages  — Get messages
    DELETE /v1/users/{user_id}/sessions/{session_id}  — Soft-delete a session

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

from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.skip(reason="Needs per-test DB isolation — see TODO")
class TestSessionCrud:
    """Full CRUD lifecycle for the ``/v1/users/{user_id}/sessions`` endpoints.

    Each test is fully self-contained:
    1. Bootstrap an org via the admin endpoint.
    2. Create a user via POST /v1/users.
    3. Exercise the session endpoint(s) under test.
    """

    # ═════════════════════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    async def _create_user(auth_client: AsyncClient, external_id: str) -> str:
        """Create a user via the API and return the user ID."""
        resp = await auth_client.post(
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

        ``SessionResponse`` fields: ``id``, ``user_id``, ``external_id``,
        ``metadata``, ``created_at``, ``updated_at``, ``closed_at``,
        ``is_deleted``.
        """
        assert "id" in body, "Missing 'id'"
        assert "user_id" in body, "Missing 'user_id'"
        assert "external_id" in body, "Missing 'external_id'"
        assert "metadata" in body, "Missing 'metadata'"
        assert "created_at" in body, "Missing 'created_at'"
        assert "updated_at" in body, "Missing 'updated_at'"
        assert "closed_at" in body, "Missing 'closed_at'"
        assert "is_deleted" in body, "Missing 'is_deleted'"

        # Validate UUIDs
        UUID(body["id"])
        UUID(body["user_id"])

        # Defaults
        assert body["is_deleted"] is False
        assert body["closed_at"] is None
        assert isinstance(body["metadata"], dict)

    # ═════════════════════════════════════════════════════════════════════
    # 1.  Create session — happy path
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.skip(reason="Needs per-test DB isolation — see TODO")
    @pytest.mark.asyncio
    async def test_create_session(self, auth_client: AsyncClient) -> None:
        """POST /sessions → 201 with a valid SessionResponse.

        The response must include ``user_id`` matching the path parameter,
        and ``external_id`` matching the request body.
        """
        user_id = await self._create_user(auth_client, "session_creator")

        response = await auth_client.post(
            f"/v1/users/{user_id}/sessions",
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
        assert body["user_id"] == user_id
        assert body["external_id"] == "session_001"
        assert body["metadata"] == {"channel": "api", "version": "1.0"}

    # ═════════════════════════════════════════════════════════════════════
    # 2.  Duplicate session external_id → 409
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_duplicate_session(
        self, auth_client: AsyncClient
    ) -> None:
        """POST /sessions with the same external_id for the same user → 409.

        The ``(user_id, external_id)`` unique constraint must prevent
        duplicate session creation.
        """
        user_id = await self._create_user(auth_client, "dup_session_user")

        # Create the first session
        resp1 = await auth_client.post(
            f"/v1/users/{user_id}/sessions",
            json={"external_id": "dup_session"},
        )
        assert resp1.status_code == 201, f"First creation failed: {resp1.text}"

        # Attempt to create a second session with the same external_id
        response = await auth_client.post(
            f"/v1/users/{user_id}/sessions",
            json={"external_id": "dup_session"},
        )
        assert response.status_code == 409, (
            f"Expected 409 for duplicate external_id, "
            f"got {response.status_code}: {response.text}"
        )

    # ═════════════════════════════════════════════════════════════════════
    # 3.  Get session → 200
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.skip(reason="Needs per-test DB isolation — see TODO")
    @pytest.mark.asyncio
    async def test_get_session(self, auth_client: AsyncClient) -> None:
        """GET /sessions/{id} → 200 with SessionResponseWithStats.

        ``SessionResponseWithStats`` extends ``SessionResponse`` with:
        ``message_count``, ``fact_count``, ``last_message_at``, ``is_open``.
        """
        user_id = await self._create_user(auth_client, "get_session_user")

        # Create a session
        created = await auth_client.post(
            f"/v1/users/{user_id}/sessions",
            json={"external_id": "get_session_test"},
        )
        assert created.status_code == 201
        session_id = created.json()["id"]

        # Fetch the session
        response = await auth_client.get(
            f"/v1/users/{user_id}/sessions/{session_id}"
        )
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        body = response.json()

        self._assert_session_response_shape(body)
        assert body["id"] == session_id
        assert body["user_id"] == user_id
        assert body["external_id"] == "get_session_test"

        # Stats fields (should be zero for a fresh session)
        assert "message_count" in body, "Missing message_count"
        assert "fact_count" in body, "Missing fact_count"
        assert "last_message_at" in body, "Missing last_message_at"
        assert "is_open" in body, "Missing is_open"
        assert body["message_count"] == 0
        assert body["fact_count"] == 0
        assert body["last_message_at"] is None
        assert body["is_open"] is True

    # ═════════════════════════════════════════════════════════════════════
    # 4.  List sessions — pagination
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.skip(reason="Needs per-test DB isolation — see TODO")
    @pytest.mark.asyncio
    async def test_list_sessions(self, auth_client: AsyncClient) -> None:
        """GET /sessions with cursor pagination.

        Create 3 sessions, fetch with limit=2:
        - Page 1: 2 items, ``has_more=True``, ``next_cursor`` is not null.
        - Page 2: 1 item,  ``has_more=False``, ``next_cursor`` is null.
        """
        user_id = await self._create_user(auth_client, "list_sesh_user")

        # Seed 3 sessions
        for i in range(3):
            resp = await auth_client.post(
                f"/v1/users/{user_id}/sessions",
                json={"external_id": f"list_session_{i}"},
            )
            assert resp.status_code == 201, f"Seed failed at index {i}"

        # Page 1
        page1 = await auth_client.get(
            f"/v1/users/{user_id}/sessions?limit=2"
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
        page2 = await auth_client.get(
            f"/v1/users/{user_id}/sessions?limit=2&cursor={body1['next_cursor']}"
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
    async def test_get_messages(self, auth_client: AsyncClient) -> None:
        """GET /sessions/{id}/messages → 200 with empty ``data`` list.

        A session with no ingested messages should return an empty array,
        not an error.
        """
        user_id = await self._create_user(auth_client, "msg_user")

        # Create a session
        created = await auth_client.post(
            f"/v1/users/{user_id}/sessions",
            json={"external_id": "no_messages_session"},
        )
        assert created.status_code == 201
        session_id = created.json()["id"]

        # Fetch messages
        response = await auth_client.get(
            f"/v1/users/{user_id}/sessions/{session_id}/messages"
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
    async def test_delete_session(self, auth_client: AsyncClient) -> None:
        """DELETE /sessions/{id} → 204, subsequent GET → 404.

        Verify the soft-delete lifecycle:
        - DELETE returns 204 No Content.
        - Fetching the same session immediately after returns 404.
        """
        user_id = await self._create_user(auth_client, "del_sesh_user")

        # Create session
        created = await auth_client.post(
            f"/v1/users/{user_id}/sessions",
            json={"external_id": "delete_me"},
        )
        assert created.status_code == 201
        session_id = created.json()["id"]

        # Delete
        delete_resp = await auth_client.delete(
            f"/v1/users/{user_id}/sessions/{session_id}"
        )
        assert delete_resp.status_code == 204, (
            f"Expected 204, got {delete_resp.status_code}: {delete_resp.text}"
        )

        # Verify it's gone
        get_resp = await auth_client.get(
            f"/v1/users/{user_id}/sessions/{session_id}"
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
        app: pytest.fixture,  # noqa: ARG002
    ) -> None:
        """A session created by org A must not be accessible by org B.

        1. Create org A → api_key A
        2. Create org B → api_key B
        3. Via org A: create a user, then create a session under that user
        4. Via org B: try to GET the session → 404 (org B cannot see org A's user)
        """
        # ── Bootstrap org A ─────────────────────────────────────────────
        transport_a = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport_a, base_url="http://test") as cli:
            resp = await cli.post(
                "/admin/organizations",
                json={"name": "Org A", "plan": "free"},
            )
            assert resp.status_code == 201
            org_a = resp.json()

        # ── Bootstrap org B ─────────────────────────────────────────────
        transport_b = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport_b, base_url="http://test") as cli:
            resp = await cli.post(
                "/admin/organizations",
                json={"name": "Org B", "plan": "free"},
            )
            assert resp.status_code == 201
            org_b = resp.json()

        # ── Org A: create user + session ────────────────────────────────
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {org_a['api_key']}"
            user_resp = await cli.post(
                "/v1/users",
                json={"external_id": "cross_tenant_user"},
            )
            assert user_resp.status_code == 201
            user_id_a = user_resp.json()["id"]

            session_resp = await cli.post(
                f"/v1/users/{user_id_a}/sessions",
                json={"external_id": "cross_tenant_session"},
            )
            assert session_resp.status_code == 201
            session_id = session_resp.json()["id"]

        # ── Org B: try to access Org A's session → 404 ─────────────────
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {org_b['api_key']}"
            get_resp = await cli.get(
                f"/v1/users/{user_id_a}/sessions/{session_id}"
            )

        assert get_resp.status_code == 404, (
            f"Org B should not be able to access Org A's session. "
            f"Got {get_resp.status_code}: {get_resp.text}"
        )

    # ═════════════════════════════════════════════════════════════════════
    # 8.  Session not found → 404
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_session_not_found(self, auth_client: AsyncClient) -> None:
        """GET /sessions with a non-existent UUID → 404."""
        user_id = await self._create_user(auth_client, "not_found_user")
        fake_session_id = "00000000-0000-0000-0000-000000000000"

        response = await auth_client.get(
            f"/v1/users/{user_id}/sessions/{fake_session_id}"
        )
        assert response.status_code == 404, (
            f"Expected 404 for non-existent session, "
            f"got {response.status_code}: {response.text}"
        )
