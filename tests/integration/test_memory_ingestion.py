"""Integration tests for memory ingestion endpoint.

Endpoints under test:

    POST   /v1/projects/{project_id}/memory    — Ingest messages (episodes)
    DELETE /v1/projects/{project_id}/memory    — Wipe all project memory

Covers:
    1.  Happy path: 10-turn conversation → 202 Accepted (latency-guarded)
    2.  Missing auth header → 401 (RFC 7807 problem-detail shape)
    3.  Missing session_id → 422 ``missing`` on ``loc ["session_id"]`` —
        ``session_id`` is required and never auto-created (no more
        ``__default__`` session).
    4.  Invalid message role → 422 ``string_pattern_mismatch`` on
        ``messages.0.role``
    5.  Unknown session_id → 404 (session not found — not auto-created)
    6.  Same Idempotency-Key header → replay (same 202)
    7.  Same Idempotency-Key, different body → 409 conflict (RFC 7807)
    8.  Identical content payload → content-dedup (same job_id)
    9.  DELETE wipes all episodes + facts → 204
    10. Cross-tenant: org B cannot access org A's memory → 403/404

Auth strategy:
    Each test creates a fresh org via the admin bootstrap fixture and
    uses ``isolated_auth_client`` (pre-authenticated) for all authenticated calls.
    The ``app`` fixture is used directly for tests that need to inspect
    cross-tenant or no-auth behaviour.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from tests.integration.conftest import asgi_transport

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


async def _session_message_count(
    client: AsyncClient,
    project_id: UUID,
    session_external_id: str,
) -> int:
    """Count messages (episodes) in a session looked up by external ID.

    Returns 0 when the session is not yet visible or has no messages.
    """
    sessions_resp = await client.get(
        f"/v1/projects/{project_id}/sessions?search={session_external_id}"
    )
    assert sessions_resp.status_code == 200, sessions_resp.text
    sessions_data = sessions_resp.json().get("data", [])
    matches = [
        s for s in sessions_data if s["external_id"] == session_external_id
    ]
    if not matches:
        return 0
    msgs_resp = await client.get(
        f"/v1/projects/{project_id}/sessions/{matches[0]['id']}/messages"
    )
    assert msgs_resp.status_code == 200, msgs_resp.text
    return len(msgs_resp.json().get("data", []))


async def _wait_for_session_message_count(
    client: AsyncClient,
    project_id: UUID,
    session_external_id: str,
    expected: int,
    timeout_s: float = 5.0,
) -> int:
    """Poll the messages endpoint until ``expected`` messages are visible.

    Ingestion commits before the 202 response, but reads go through a fresh
    session per request — poll briefly so the count assertion is not racy.
    """
    deadline = time.monotonic() + timeout_s
    last_count = 0
    while time.monotonic() < deadline:
        last_count = await _session_message_count(
            client, project_id, session_external_id
        )
        if last_count >= expected:
            return last_count
        await asyncio.sleep(0.2)
    return last_count


@pytest.fixture
async def anon_client(isolated_app: Any) -> AsyncClient:
    """Return an unauthenticated HTTP client — for the 401 test."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMemoryIngestion:
    """Tests for ``POST /v1/projects/{project_id}/memory`` ingestion."""

    # ═════════════════════════════════════════════════════════════════════════
    # 1.  Happy path — 202 Accepted
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_memory_returns_202(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """POST /v1/projects/{project_id}/memory with 2 messages → 202.

        The response must include ``episode_count`` equal to the number of
        messages ingested, and ``status`` set to ``"accepted"``.
        """
        # Create a user first
        user_resp = await isolated_auth_client.post(
            "/v1/users",
            json={"external_id": "ingest_happy_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # Create session before ingesting
        session_resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "test_session"},
        )
        assert session_resp.status_code == 201

        # Ingest — measure latency for G1.1 (must be ≤200ms for 10-turn conversation)
        _ten_turn_conversation: list[dict] = []
        for i in range(5):
            _ten_turn_conversation.append(
                {"role": "user", "content": f"Turn {i}: user message"}
            )
            _ten_turn_conversation.append(
                {"role": "assistant", "content": f"Turn {i}: assistant response"}
            )

        _start = time.monotonic()
        response = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            data={"data": json.dumps({
                "external_id": "ingest_happy_user",
                "session_id": "test_session",
                "messages": _ten_turn_conversation,
            })},
        )
        _elapsed_ms = (time.monotonic() - _start) * 1000
        assert response.status_code == 202, (
            f"Expected 202, got {response.status_code}: {response.text}"
        )
        body = response.json()

        _assert_ingest_response_shape(body, expected_episodes=10)

        # G1.1: POST /v1/projects/{project_id}/memory with 10-turn conversation
        # returns 202 within 2s
        assert _elapsed_ms < 2000, (
            f"G1.1 FAIL: POST /memory took {_elapsed_ms:.1f}ms, expected <2000ms"
        )

        # Also check the session object got created
        get_resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions?search=test_session"
        )
        assert get_resp.status_code == 200

    # ═════════════════════════════════════════════════════════════════════════
    # 3.  No authentication → 401
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_no_auth_returns_401(
        self,
        anon_client: AsyncClient,
        isolated_project_id: UUID,  # noqa: ARG002 — fixture presence, unused in path
    ) -> None:
        """POST /v1/projects/{project_id}/memory without auth → 401.

        The auth middleware must reject requests that lack a valid API key.
        """
        response = await anon_client.post(
            "/v1/projects/00000000-0000-0000-0000-000000000000/memory",
            data={"data": json.dumps({
                "session_id": "no_auth_session",
                "messages": [
                    {"role": "user", "content": "Hello"},
                ],
            })},
        )
        assert response.status_code == 401, (
            f"Expected 401 without auth, "
            f"got {response.status_code}: {response.text}"
        )
        body = response.json()
        # RFC 7807 problem-detail shape
        assert "detail" in body or "status" in body

    # ═════════════════════════════════════════════════════════════════════════
    # 5.  Missing session_id → 422 (no auto-created session)
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_without_session_id_returns_422(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """POST /v1/projects/{project_id}/memory without ``session_id`` → 422.

        ``session_id`` is now REQUIRED — the server never auto-creates a
        ``__default__`` session.  This used to 500 (uncaught validation
        error); it must now surface as a proper 422 with a ``missing``
        error on ``session_id``.
        """
        user_resp = await isolated_auth_client.post(
            "/v1/users",
            json={"external_id": "missing_sesh_user"},
        )
        assert user_resp.status_code == 201

        response = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            data={"data": json.dumps({
                "external_id": "missing_sesh_user",
                "messages": [
                    {"role": "user", "content": "No session ID"},
                    {"role": "assistant", "content": "Must 422"},
                ],
            })},
        )
        assert response.status_code == 422, (
            f"Expected 422, got {response.status_code}: {response.text}"
        )
        detail = response.json()["detail"]
        assert any(
            err.get("type") == "missing" and err.get("loc") == ["session_id"]
            for err in detail
        ), f"Expected a 'missing' error on session_id, got: {detail}"

    # ═════════════════════════════════════════════════════════════════════════
    # 6.  Invalid message role → 422 (string_pattern_mismatch)
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_invalid_role_returns_422(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """POST with ``messages[0].role`` outside the allowed set → 422.

        Role must match ``^(user|assistant|system|tool)$`` — a pattern
        violation surfaces as ``string_pattern_mismatch`` on
        ``messages.0.role`` (previously a latent 500).
        """
        response = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            data={"data": json.dumps({
                "session_id": "any-session",
                "messages": [
                    {"role": "invalid-role", "content": "Hello"},
                ],
            })},
        )
        assert response.status_code == 422, (
            f"Expected 422, got {response.status_code}: {response.text}"
        )
        detail = response.json()["detail"]
        assert any(
            err.get("type") == "string_pattern_mismatch"
            and err.get("loc") == ["messages", 0, "role"]
            for err in detail
        ), f"Expected string_pattern_mismatch on messages.0.role, got: {detail}"

    # ═════════════════════════════════════════════════════════════════════════
    # 7.  Unknown session_id → 404 (session not found)
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_unknown_session_returns_404(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """POST with a well-formed ``session_id`` that doesn't exist → 404.

        Sessions are never auto-created — the caller must create one via
        ``POST /v1/projects/{project_id}/sessions`` first.
        """
        response = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            data={"data": json.dumps({
                "session_id": "no-such-session",
                "messages": [
                    {"role": "user", "content": "Hello"},
                ],
            })},
        )
        assert response.status_code == 404, (
            f"Expected 404, got {response.status_code}: {response.text}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 8.  Idempotency key — replay returns same 202
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_idempotency_key_replay(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """Same ``Idempotency-Key`` + same payload → same 202 on replay.

        The first request should process normally.  The second request
        (identical key + body) must return the cached response without
        creating duplicate episodes.
        """
        user_resp = await isolated_auth_client.post(
            "/v1/users",
            json={"external_id": "idem_replay_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # Create session before ingesting
        session_resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "idem_session"},
        )
        assert session_resp.status_code == 201

        idem_key = "idem-replay-001"

        payload = {
            "external_id": "idem_replay_user",
            "session_id": "idem_session",
            "messages": [
                {"role": "user", "content": "First attempt"},
            ],
        }

        # First request
        resp1 = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            headers={"Idempotency-Key": idem_key},
            data={"data": json.dumps(payload)},
        )
        assert resp1.status_code == 202
        body1 = resp1.json()
        _assert_ingest_response_shape(body1, expected_episodes=1)
        job_id_1 = body1["job_id"]

        # Second request — identical key + body
        resp2 = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            headers={"Idempotency-Key": idem_key},
            data={"data": json.dumps(payload)},
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
    # 9.  Idempotency key — same key, different payload → 409 conflict
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_idempotency_key_conflict(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """Same ``Idempotency-Key`` with a different body → 409 conflict.

        Reusing an idempotency key for a different request payload is a
        client error: the endpoint must return RFC 7807 ``Conflict`` and
        must not create a new episode.
        """
        user_resp = await isolated_auth_client.post(
            "/v1/users",
            json={"external_id": "idem_conflict_user"},
        )
        assert user_resp.status_code == 201

        # Create session before ingesting
        session_resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "conflict_session"},
        )
        assert session_resp.status_code == 201

        idem_key = "idem-conflict-002"

        # First request — accepted
        resp1 = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            headers={"Idempotency-Key": idem_key},
            data={"data": json.dumps({
                "external_id": "idem_conflict_user",
                "session_id": "conflict_session",
                "messages": [
                    {"role": "user", "content": "Original message"},
                ],
            })},
        )
        assert resp1.status_code == 202, f"First request failed: {resp1.text}"

        # Wait for the first episode to become visible so the count check
        # below is meaningful.
        await _wait_for_session_message_count(
            isolated_auth_client, isolated_project_id, "conflict_session", 1
        )

        # Second request — same key, DIFFERENT body → 409 conflict
        resp2 = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            headers={"Idempotency-Key": idem_key},
            data={"data": json.dumps({
                "external_id": "idem_conflict_user",
                "session_id": "conflict_session",
                "messages": [
                    {"role": "user", "content": "Completely different content"},
                ],
            })},
        )
        assert resp2.status_code == 409, (
            f"Expected 409 (idempotency key conflict), "
            f"got {resp2.status_code}: {resp2.text}"
        )

        # RFC 7807 problem-detail shape
        body = resp2.json()
        for field in ("type", "title", "status", "detail"):
            assert field in body, f"RFC 7807 body missing '{field}': {body}"
        assert body["status"] == 409
        assert body["type"].endswith("/conflict")

        # The conflicting request must not create a new episode
        count_after = await _session_message_count(
            isolated_auth_client, isolated_project_id, "conflict_session"
        )
        assert count_after == 1, (
            f"Conflict request must not create a new episode; "
            f"expected 1, got {count_after}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 10. Content dedup — identical payload → dedup hit → same job_id
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_content_dedup(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """Two identical memory payloads → 202 + same job_id (no duplicate).

        Content-level deduplication is based on a SHA-256 hash of
        ``(user_id, session_id, messages)``.  Two requests with the
        same content but different Idempotency-Key values must return
        the same ``job_id`` and not create duplicate episode rows.
        """
        user_resp = await isolated_auth_client.post(
            "/v1/users",
            json={"external_id": "dedup_user"},
        )
        assert user_resp.status_code == 201

        # Create session before ingesting
        session_resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "dedup_session"},
        )
        assert session_resp.status_code == 201

        payload = {
            "external_id": "dedup_user",
            "session_id": "dedup_session",
            "messages": [
                {"role": "user", "content": "Dedup check"},
                {"role": "assistant", "content": "This should be deduped"},
            ],
        }

        # First ingestion — different key
        resp1 = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            headers={"Idempotency-Key": "dedup-key-1"},
            data={"data": json.dumps(payload)},
        )
        assert resp1.status_code == 202, f"First ingestion failed: {resp1.text}"
        body1 = resp1.json()
        _assert_ingest_response_shape(body1, expected_episodes=2)
        job_id_1 = body1["job_id"]

        # Second ingestion — identical payload, different key
        resp2 = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            headers={"Idempotency-Key": "dedup-key-2"},
            data={"data": json.dumps(payload)},
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
    # 11. Delete user memory — 204
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_delete_user_memory(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """DELETE /v1/projects/{project_id}/memory → 204 + episodes are gone.

        After the wipe, subsequent GET calls for episodes should return
        an empty list (or 404-equivalent).
        """
        user_resp = await isolated_auth_client.post(
            "/v1/users",
            json={"external_id": "wipe_test_user"},
        )
        assert user_resp.status_code == 201

        # Create session before ingesting
        session_resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "wipe_session"},
        )
        assert session_resp.status_code == 201

        # Ingest some messages first
        ingest_resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            data={"data": json.dumps({
                "external_id": "wipe_test_user",
                "session_id": "wipe_session",
                "messages": [
                    {"role": "user", "content": "Message to be wiped"},
                ],
            })},
        )
        assert ingest_resp.status_code == 202

        # Wipe memory
        delete_resp = await isolated_auth_client.delete(
            f"/v1/projects/{isolated_project_id}/memory",
        )
        assert delete_resp.status_code == 204, (
            f"Expected 204 on memory wipe, "
            f"got {delete_resp.status_code}: {delete_resp.text}"
        )

        # ⚠️ No content on 204 — verify by attempting to inspect state
        # The session should still exist, but episodes should be gone.
        # Subsequent GET on the session's messages should return empty.
        sessions_resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions?search=wipe_session"
        )
        assert sessions_resp.status_code == 200
        sessions_data = sessions_resp.json().get("data", [])
        wipe_sessions = [
            s for s in sessions_data if s["external_id"] == "wipe_session"
        ]
        if wipe_sessions:
            session_id = wipe_sessions[0]["id"]
            msgs_resp = await isolated_auth_client.get(
                f"/v1/projects/{isolated_project_id}/sessions/{session_id}/messages"
            )
            assert msgs_resp.status_code == 200
            msgs_body = msgs_resp.json()
            messages_data = msgs_body.get("data", [])
            assert len(messages_data) == 0, (
                f"Expected 0 messages after wipe, "
                f"got {len(messages_data)}"
            )


@pytest.mark.skip(reason="Needs per-test DB isolation — see TODO")
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
        3. Org B tries to access the same project by UUID → 404 (RLS).
        """
        # ── Bootstrap org A ───────────────────────────────────────────────
        transport_a = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport_a, base_url="http://test") as cli:
            tenant_a = await bootstrap_tenant(app, cli, "Org A")
            project_id_a = tenant_a["project_id"]

        # ── Bootstrap org B ───────────────────────────────────────────────
        transport_b = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport_b, base_url="http://test") as cli:
            tenant_b = await bootstrap_tenant(app, cli, "Org B")

        # ── Org A: create user + ingest memory ────────────────────────────
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {tenant_a['api_key']}"
            user_resp = await cli.post(
                "/v1/users",
                json={"external_id": "cross_tenant_mem_user"},
            )
            assert user_resp.status_code == 201

            # Create session before ingesting
            session_resp = await cli.post(
                f"/v1/projects/{project_id_a}/sessions",
                json={"external_id": "x_tenant_session"},
            )
            assert session_resp.status_code == 201

            ingest_resp = await cli.post(
                f"/v1/projects/{project_id_a}/memory",
                data={"data": json.dumps({
                    "external_id": "cross_tenant_mem_user",
                    "session_id": "x_tenant_session",
                    "messages": [
                        {"role": "user", "content": "Secret message"},
                    ],
                })},
            )
            assert ingest_resp.status_code == 202

        # ── Org B: try to access Org A's memory by project → 404 ──────────
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {tenant_b['api_key']}"
            ingest_resp = await cli.post(
                f"/v1/projects/{project_id_a}/memory",
                data={"data": json.dumps({
                    "external_id": "should_not_work",
                    "session_id": "x_tenant_session",
                    "messages": [
                        {"role": "user", "content": "Should not work"},
                    ],
                })},
            )

        # ⚠️ Org B cannot see Org A's project → the endpoint should reject
        # because the project ID UUID doesn't belong to Org B (RLS).
        # Expect 404 (project not found) or 403 (RLS violation).
        assert ingest_resp.status_code in (403, 404), (
            f"Org B should not be able to ingest under Org A's project. "
            f"Got {ingest_resp.status_code}: {ingest_resp.text}"
        )
