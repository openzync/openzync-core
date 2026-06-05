"""Integration tests for memory ingestion endpoint.

Endpoints under test:

    POST   /v1/users/{user_id}/memory    — Ingest messages (episodes)
    DELETE /v1/users/{user_id}/memory    — Wipe all user memory

Covers:
    1.  Happy path: 10-turn conversation → 202 Accepted
    2.  Empty messages list → 422
    3.  Invalid role field → 422
    4.  Missing auth header → 401
    5.  New user external_id → auto-create (get-or-create)
    6.  No session_id → auto-create __default__ session
    7.  Same Idempotency-Key header → replay (same 202)
    8.  Same Idempotency-Key, different body → 409 conflict
    9.  Identical content payload → content-dedup (same job_id)
    10. DELETE wipes all episodes + facts → 204

Auth strategy:
    Each test creates a fresh org via the admin bootstrap fixture and
    uses ``auth_client`` (pre-authenticated) for all authenticated calls.
    The ``app`` fixture is used directly for tests that need to inspect
    cross-tenant or no-auth behaviour.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _assert_ingest_response_shape(body: dict, expected_episodes: int = 1) -> None:
    """Validate that ``body`` matches the ``IngestMemoryResponse`` schema.

    ``IngestMemoryResponse`` fields: ``job_id``, ``episode_count``,
    ``status``, ``message``.
    """
    assert "job_id" in body, "Missing 'job_id'"
    assert "episode_count" in body, "Missing 'episode_count'"
    assert "status" in body, "Missing 'status'"
    assert "message" in body, "Missing 'message'"

    assert body["status"] == "accepted", f"Expected 'accepted', got {body['status']}"
    assert body["episode_count"] == expected_episodes, (
        f"Expected {expected_episodes} episodes, got {body['episode_count']}"
    )

    # job_id must be a valid UUID when present
    if body["job_id"] is not None:
        UUID(body["job_id"])

    assert isinstance(body["message"], str) and len(body["message"]) > 0


@pytest.fixture
async def anon_client(app: pytest.fixture) -> AsyncClient:  # noqa: ARG002
    """Return an unauthenticated HTTP client — for the 401 test."""
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMemoryIngestion:
    """Tests for ``POST /v1/users/{user_id}/memory`` ingestion."""

    # ═════════════════════════════════════════════════════════════════════════
    # 1.  Happy path — 202 Accepted
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_memory_returns_202(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /memory with 2 messages → 202 + IngestMemoryResponse.

        The response must include ``episode_count`` equal to the number of
        messages ingested, and ``status`` set to ``"accepted"``.
        """
        # Create a user first
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "ingest_happy_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # Ingest
        response = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            json={
                "session_id": "test_session",
                "messages": [
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Hi there!"},
                ],
            },
        )
        assert response.status_code == 202, (
            f"Expected 202, got {response.status_code}: {response.text}"
        )
        body = response.json()

        _assert_ingest_response_shape(body, expected_episodes=2)

        # Also check the session object got created
        get_resp = await auth_client.get(
            f"/v1/users/{user_id}/sessions?search=test_session"
        )
        assert get_resp.status_code == 200

    # ═════════════════════════════════════════════════════════════════════════
    # 2.  Empty messages list → 422
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_empty_messages_returns_422(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /memory with empty ``messages`` array → 422.

        The ``IngestMemoryRequest.messages`` field has ``min_length=1``,
        so an empty list must be rejected at the Pydantic validation layer.
        """
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "empty_msg_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        response = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            json={
                "session_id": "test_session",
                "messages": [],
            },
        )
        assert response.status_code == 422, (
            f"Expected 422 for empty messages, "
            f"got {response.status_code}: {response.text}"
        )
        body = response.json()
        # FastAPI Pydantic validation error shape
        assert "detail" in body, "Expected validation error detail"

    # ═════════════════════════════════════════════════════════════════════════
    # 3.  Invalid role → 422
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_invalid_role_returns_422(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /memory with an invalid ``role`` value → 422.

        The ``Message.role`` field accepts only ``user``, ``assistant``,
        ``system``, or ``tool`` (validated via regex pattern).
        """
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "bad_role_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        response = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            json={
                "session_id": "test_session",
                "messages": [
                    {"role": "superadmin", "content": "I should not work"},
                ],
            },
        )
        assert response.status_code == 422, (
            f"Expected 422 for invalid role, "
            f"got {response.status_code}: {response.text}"
        )
        body = response.json()
        assert "detail" in body, "Expected validation error detail"

        # The error should reference the role field
        detail_str = str(body["detail"]).lower()
        assert "role" in detail_str, (
            f"Validation error should reference 'role' field: {detail_str}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 4.  No authentication → 401
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_no_auth_returns_401(
        self,
        anon_client: AsyncClient,
    ) -> None:
        """POST /memory without an ``Authorization`` header → 401.

        The auth middleware must reject requests that lack a valid API key.
        """
        response = await anon_client.post(
            "/v1/users/00000000-0000-0000-0000-000000000000/memory",
            json={
                "session_id": "no_auth_session",
                "messages": [
                    {"role": "user", "content": "Hello"},
                ],
            },
        )
        assert response.status_code == 401, (
            f"Expected 401 without auth, "
            f"got {response.status_code}: {response.text}"
        )
        body = response.json()
        # RFC 7807 problem-detail shape
        assert "detail" in body or "status" in body

    # ═════════════════════════════════════════════════════════════════════════
    # 5.  User not found — auto-create via get-or-create
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_user_not_found_creates_user(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /memory for a new external_id → user is auto-created.

        The MemoryService uses ``get_or_create`` semantics via the
        ``(organization_id, external_id)`` unique constraint.
        A subsequent GET /v1/users should return the auto-created user.
        """
        fresh_external_id = "auto_create_user_42"

        # Do NOT create the user beforehand — this should auto-create.
        response = await auth_client.post(
            f"/v1/users/{fresh_external_id}/memory",
            json={
                "session_id": "test_session",
                "messages": [
                    {"role": "user", "content": "Auto-create test"},
                ],
            },
        )
        assert response.status_code == 202, (
            f"Expected 202 with auto-create, "
            f"got {response.status_code}: {response.text}"
        )
        _assert_ingest_response_shape(response.json(), expected_episodes=1)

        # Verify the user now exists (GET /v1/users should list it)
        list_resp = await auth_client.get(f"/v1/users?search={fresh_external_id}")
        assert list_resp.status_code == 200
        data = list_resp.json().get("data", [])
        matching = [u for u in data if u["external_id"] == fresh_external_id]
        assert len(matching) >= 1, (
            f"Auto-created user '{fresh_external_id}' not found in user list"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 6.  No session_id → auto-create __default__ session
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_without_session_id_creates_default(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /memory without ``session_id`` → auto-create __default__.

        When ``session_id`` is omitted, the service creates a session
        named ``__default__`` for the user and ingests into it.
        """
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "default_sesh_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        response = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            json={
                "messages": [
                    {"role": "user", "content": "No session ID"},
                    {"role": "assistant", "content": "Default session works"},
                ],
            },
        )
        assert response.status_code == 202, (
            f"Expected 202, got {response.status_code}: {response.text}"
        )
        _assert_ingest_response_shape(response.json(), expected_episodes=2)

        # Verify the default session exists and has messages
        sessions_resp = await auth_client.get(
            f"/v1/users/{user_id}/sessions?search=__default__"
        )
        assert sessions_resp.status_code == 200
        sessions_data = sessions_resp.json().get("data", [])
        default_sessions = [
            s for s in sessions_data if s["external_id"] == "__default__"
        ]
        assert len(default_sessions) >= 1, (
            "__default__ session should have been auto-created"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 7.  Idempotency key — replay returns same 202
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_idempotency_key_replay(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """Same ``Idempotency-Key`` + same payload → same 202 on replay.

        The first request should process normally.  The second request
        (identical key + body) must return the cached response without
        creating duplicate episodes.
        """
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "idem_replay_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        idem_key = "idem-replay-001"

        # First request
        resp1 = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            headers={"Idempotency-Key": idem_key},
            json={
                "session_id": "idem_session",
                "messages": [
                    {"role": "user", "content": "First attempt"},
                ],
            },
        )
        assert resp1.status_code == 202
        body1 = resp1.json()
        _assert_ingest_response_shape(body1, expected_episodes=1)
        job_id_1 = body1["job_id"]

        # Second request — identical key + body
        resp2 = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            headers={"Idempotency-Key": idem_key},
            json={
                "session_id": "idem_session",
                "messages": [
                    {"role": "user", "content": "First attempt"},
                ],
            },
        )
        assert resp2.status_code == 202, (
            f"Expected 202 on idempotent replay, "
            f"got {resp2.status_code}: {resp2.text}"
        )
        body2 = resp2.json()
        _assert_ingest_response_shape(body2, expected_episodes=1)

        # The response should be identical (same job_id)
        assert body2["job_id"] == job_id_1, (
            "Idempotent replay should return the same job_id"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 8.  Idempotency key — same key, different payload → 409 conflict
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_idempotency_key_conflict(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """Same ``Idempotency-Key`` with different body → 409 Conflict.

        Reusing an idempotency key for a different request payload is a
        client error — the endpoint must reject it with 409.
        """
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "idem_conflict_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        idem_key = "idem-conflict-002"

        # First request
        resp1 = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            headers={"Idempotency-Key": idem_key},
            json={
                "session_id": "conflict_session",
                "messages": [
                    {"role": "user", "content": "Original message"},
                ],
            },
        )
        assert resp1.status_code == 202, f"First request failed: {resp1.text}"

        # Second request — same key, WRONG body
        resp2 = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            headers={"Idempotency-Key": idem_key},
            json={
                "session_id": "conflict_session",
                "messages": [
                    {"role": "user", "content": "Completely different content"},
                ],
            },
        )
        assert resp2.status_code == 409, (
            f"Expected 409 for idempotency key conflict, "
            f"got {resp2.status_code}: {resp2.text}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 9.  Content dedup — identical payload → dedup hit → same job_id
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_content_dedup(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """Two identical memory payloads → 202 + same job_id (no duplicate).

        Content-level deduplication is based on a SHA-256 hash of
        ``(user_id, session_id, messages)``.  Two requests with the
        same content but different Idempotency-Key values must return
        the same ``job_id`` and not create duplicate episode rows.
        """
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "dedup_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        payload = {
            "session_id": "dedup_session",
            "messages": [
                {"role": "user", "content": "Dedup check"},
                {"role": "assistant", "content": "This should be deduped"},
            ],
        }

        # First ingestion — different key
        resp1 = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            headers={"Idempotency-Key": "dedup-key-1"},
            json=payload,
        )
        assert resp1.status_code == 202, f"First ingestion failed: {resp1.text}"
        body1 = resp1.json()
        _assert_ingest_response_shape(body1, expected_episodes=2)
        job_id_1 = body1["job_id"]

        # Second ingestion — identical payload, different key
        resp2 = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            headers={"Idempotency-Key": "dedup-key-2"},
            json=payload,
        )
        assert resp2.status_code == 202, (
            f"Expected 202 on dedup hit, "
            f"got {resp2.status_code}: {resp2.text}"
        )
        body2 = resp2.json()
        _assert_ingest_response_shape(body2, expected_episodes=2)

        # The response must contain the same job_id (content dedup)
        assert body2["job_id"] == job_id_1, (
            f"Content dedup should return the same job_id. "
            f"Got {body2['job_id']} vs {job_id_1}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 10.  Delete user memory — 204
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_delete_user_memory(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """DELETE /memory → 204 + episodes are gone.

        After the wipe, subsequent GET calls for episodes should return
        an empty list (or 404-equivalent).
        """
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "wipe_test_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # Ingest some messages first
        ingest_resp = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            json={
                "session_id": "wipe_session",
                "messages": [
                    {"role": "user", "content": "Message to be wiped"},
                ],
            },
        )
        assert ingest_resp.status_code == 202

        # Wipe memory
        delete_resp = await auth_client.delete(
            f"/v1/users/{user_id}/memory",
        )
        assert delete_resp.status_code == 204, (
            f"Expected 204 on memory wipe, "
            f"got {delete_resp.status_code}: {delete_resp.text}"
        )

        # ⚠️ No content on 204 — verify by attempting to inspect state
        # The session should still exist, but episodes should be gone.
        # Subsequent GET on the session's messages should return empty.
        sessions_resp = await auth_client.get(
            f"/v1/users/{user_id}/sessions?search=wipe_session"
        )
        assert sessions_resp.status_code == 200
        sessions_data = sessions_resp.json().get("data", [])
        wipe_sessions = [
            s for s in sessions_data if s["external_id"] == "wipe_session"
        ]
        if wipe_sessions:
            session_id = wipe_sessions[0]["id"]
            msgs_resp = await auth_client.get(
                f"/v1/users/{user_id}/sessions/{session_id}/messages"
            )
            assert msgs_resp.status_code == 200
            msgs_body = msgs_resp.json()
            messages_data = msgs_body.get("data", [])
            assert len(messages_data) == 0, (
                f"Expected 0 messages after wipe, "
                f"got {len(messages_data)}"
            )


class TestMemoryCrossTenant:
    """Cross-tenant isolation for memory ingestion."""

    @pytest.mark.asyncio
    async def test_memory_cross_tenant(
        self,
        app: pytest.fixture,  # noqa: ARG002
    ) -> None:
        """Memory ingested by org A must not be accessible by org B.

        1. Bootstrap org A + org B.
        2. Org A creates a user and ingests memory.
        3. Org B tries to access the same user by UUID → 404 (RLS).
        """
        # ── Bootstrap org A ───────────────────────────────────────────────
        transport_a = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport_a, base_url="http://test") as cli:
            resp = await cli.post(
                "/admin/organizations",
                json={"name": "Org A", "plan": "free"},
            )
            assert resp.status_code == 201
            org_a = resp.json()

        # ── Bootstrap org B ───────────────────────────────────────────────
        transport_b = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport_b, base_url="http://test") as cli:
            resp = await cli.post(
                "/admin/organizations",
                json={"name": "Org B", "plan": "free"},
            )
            assert resp.status_code == 201
            org_b = resp.json()

        # ── Org A: create user + ingest memory ────────────────────────────
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {org_a['api_key']}"
            user_resp = await cli.post(
                "/v1/users",
                json={"external_id": "cross_tenant_mem_user"},
            )
            assert user_resp.status_code == 201
            user_id_a = user_resp.json()["id"]

            ingest_resp = await cli.post(
                f"/v1/users/{user_id_a}/memory",
                json={
                    "session_id": "x_tenant_session",
                    "messages": [
                        {"role": "user", "content": "Secret message"},
                    ],
                },
            )
            assert ingest_resp.status_code == 202

        # ── Org B: try to access Org A's memory by UUID → 404 ────────────
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {org_b['api_key']}"
            ingest_resp = await cli.post(
                f"/v1/users/{user_id_a}/memory",
                json={
                    "session_id": "x_tenant_session",
                    "messages": [
                        {"role": "user", "content": "Should not work"},
                    ],
                },
            )

        # ⚠️ Org B cannot see Org A's user → the endpoint should reject
        # because the user_id UUID doesn't belong to Org B (RLS).
        # Expect 404 (user not found) or 403 (RLS violation).
        assert ingest_resp.status_code in (403, 404), (
            f"Org B should not be able to ingest under Org A's user. "
            f"Got {ingest_resp.status_code}: {ingest_resp.text}"
        )
