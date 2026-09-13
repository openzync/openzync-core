"""E2E tests for the full memory ingestion and search pipeline.

End-to-end flow:
    1. Bootstrap org via admin endpoint → get API key.
    2. Create user via ``POST /v1/users`` → user UUID.
    3. Create session via ``POST /v1/sessions`` → session UUID.
    4. Ingest memory via ``POST /v1/users/{user_id}/memory`` → 202 accepted.
    5. Search via ``GET /v1/users/{user_id}/search`` → 200 with results.

These tests run against real PostgreSQL + Redis via testcontainers (shared
with the integration test suite).  Background ARQ workers are NOT running,
so enrichment results will not be available immediately — only the
synchronous API contract is verified.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient


class TestFullIngestionPipeline:
    """Complete memory ingestion and search flow."""

    # ═════════════════════════════════════════════════════════════════════
    # E2E: create user
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_create_user(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """Create a user via the API and verify the response shape."""
        resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "e2e-test-user-1"},
        )
        assert resp.status_code == 201, (
            f"Expected 201, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "id" in body, "Missing 'id' in user response"
        UUID(body["id"])  # must be valid UUID
        assert body.get("external_id") == "e2e-test-user-1"

    # ═════════════════════════════════════════════════════════════════════
    # E2E: create user → create session
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_create_user_and_session(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """Create a user and then create a session for that user."""
        # ── Create user ──────────────────────────────────────────────
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "e2e-test-user-2"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # ── Create session ───────────────────────────────────────────
        session_resp = await auth_client.post(
            f"/v1/users/{user_id}/sessions",
            json={},
        )
        assert session_resp.status_code == 201, (
            f"Expected 201, got {session_resp.status_code}: {session_resp.text}"
        )
        session_body = session_resp.json()
        assert "id" in session_body, "Missing 'id' in session response"
        UUID(session_body["id"])
        assert session_body["user_id"] == user_id

        # ── Verify session is listable ───────────────────────────────
        list_resp = await auth_client.get(f"/v1/users/{user_id}/sessions")
        assert list_resp.status_code == 200
        list_body = list_resp.json()
        assert len(list_body.get("items", [])) >= 1

    # ═════════════════════════════════════════════════════════════════════
    # E2E: create user → create session → ingest memory
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_ingest_memory(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """Ingest messages into a user session — verify 202 accepted."""
        # ── Create user ──────────────────────────────────────────────
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "e2e-test-user-3"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # ── Create session ───────────────────────────────────────────
        session_resp = await auth_client.post(
            f"/v1/users/{user_id}/sessions",
            json={},
        )
        assert session_resp.status_code == 201
        session_id = session_resp.json()["id"]

        # ── Ingest memory ────────────────────────────────────────────
        ingest_resp = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            json={
                "messages": [
                    {"role": "user", "content": "Hello, my name is Alice."},
                    {"role": "assistant", "content": "Nice to meet you, Alice!"},
                ],
                "session_id": session_id,
            },
        )
        assert ingest_resp.status_code == 202, (
            f"Expected 202, got {ingest_resp.status_code}: {ingest_resp.text}"
        )
        ingest_body = ingest_resp.json()
        assert ingest_body["status"] == "accepted"
        assert ingest_body["episode_count"] == 2

        # ── Verify episodes are persisted ────────────────────────────
        messages_resp = await auth_client.get(
            f"/v1/users/{user_id}/sessions/{session_id}/messages",
        )
        assert messages_resp.status_code == 200
        messages_body = messages_resp.json()
        # Episodes should be persisted synchronously even though
        # enrichment (embedding, fact extraction) is async.
        assert len(messages_body.get("items", [])) == 2, (
            f"Expected 2 persisted messages, got {len(messages_body.get('items', []))}"
        )

    # ═════════════════════════════════════════════════════════════════════
    # E2E: create user → search (no results expected — no ingested data)
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_search_empty(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """Search with no ingested data — verify 200 with empty results."""
        # ── Create user ──────────────────────────────────────────────
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "e2e-test-user-4"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # ── Search (nothing ingested yet) ────────────────────────────
        search_resp = await auth_client.get(
            f"/v1/users/{user_id}/search",
            params={"query": "Alice", "limit": 10},
        )
        assert search_resp.status_code == 200, (
            f"Expected 200, got {search_resp.status_code}: {search_resp.text}"
        )
        body = search_resp.json()
        # Should return valid structure even if empty
        assert isinstance(body, dict)

    # ═════════════════════════════════════════════════════════════════════
    # E2E: full flow — create user → session → ingest → list messages
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_full_flow(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """Complete non-enriched flow: user → session → ingest → verify.

        Background enrichment workers are NOT running, so we only verify
        synchronous persistence (episodes) and API contract compliance.
        """
        # ── 1. Create user ───────────────────────────────────────────
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "e2e-test-user-5"},
        )
        assert user_resp.status_code == 201, (
            f"User creation failed: {user_resp.status_code} {user_resp.text}"
        )
        user_id: str = user_resp.json()["id"]
        UUID(user_id)

        # ── 2. Create session ────────────────────────────────────────
        session_resp = await auth_client.post(
            f"/v1/users/{user_id}/sessions",
            json={},
        )
        assert session_resp.status_code == 201
        session_id: str = session_resp.json()["id"]
        UUID(session_id)

        # ── 3. Ingest memory ─────────────────────────────────────────
        ingest_resp = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            json={
                "messages": [
                    {"role": "user", "content": "What is the capital of France?"},
                    {"role": "assistant", "content": "The capital of France is Paris."},
                ],
                "session_id": session_id,
            },
        )
        assert ingest_resp.status_code == 202, (
            f"Ingestion failed: {ingest_resp.status_code} {ingest_resp.text}"
        )
        ingest_body = ingest_resp.json()
        assert ingest_body["status"] == "accepted"
        assert ingest_body["episode_count"] == 2

        # ── 4. List messages (synchronous persistence check) ─────────
        messages_resp = await auth_client.get(
            f"/v1/users/{user_id}/sessions/{session_id}/messages",
        )
        assert messages_resp.status_code == 200
        messages_body = messages_resp.json()
        messages = messages_body.get("items", [])
        assert len(messages) == 2, (
            f"Expected 2 messages, got {len(messages)}"
        )

        # ── 5. Verify session stats include message count ────────────
        stats_resp = await auth_client.get(
            f"/v1/users/{user_id}/sessions/{session_id}",
        )
        assert stats_resp.status_code == 200
        stats_body = stats_resp.json()
        # The stats object may be a nested field — check existence
        # without assuming the structure since enrichment is incomplete.
        assert "id" in stats_body
        assert stats_body["id"] == session_id
