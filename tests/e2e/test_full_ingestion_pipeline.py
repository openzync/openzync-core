"""E2E tests for the full memory ingestion and search pipeline.

End-to-end flow (project-scoped contract):
    1. Tenant bootstrap via the ``org_and_key`` fixture → project + API key.
    2. Create user via ``POST /v1/users`` (admin JWT — project keys carry
       project-only permissions and cannot manage org users).
    3. Create session via ``POST /v1/projects/{id}/sessions`` → session UUID.
    4. Ingest memory via multipart ``POST /v1/projects/{id}/memory`` → 202.
    5. Read back via ``GET .../sessions/{id}/messages`` and project search.

These tests run against real PostgreSQL + Redis via testcontainers (shared
with the integration test suite).  Background ARQ workers are NOT running,
so enrichment results will not be available immediately — only the
synchronous API contract is verified.
"""

# F811: the integration fixtures re-exported below are also requested as
# parameters by the tests in this module — same name, no real collision.
# ruff: noqa: F811
from __future__ import annotations

import json
from uuid import UUID

import pytest
from httpx import AsyncClient

from tests.integration.conftest import (  # noqa: F401  (fixtures are re-exported)
    admin_client,
    app,
    auth_client,
    engine,
    org_and_key,
    redis_client,
)


@pytest.fixture()
def _search_dispatcher(app) -> None:
    """Wire the graph-backend dispatcher (``app`` skips lifespan).

    Mirrors ``tests/security/test_cross_tenant.py``: without the
    dispatcher the hybrid search leg raises ``AttributeError`` instead
    of serving empty results.
    """
    from core.graph_backend import init_dispatcher

    app.state.graph_backend_dispatcher = init_dispatcher()


@pytest.fixture()
def _fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the query embedder at canonical-dim zero vectors."""
    from tests.security.test_cross_tenant import _patch_embedder

    _patch_embedder(monkeypatch)


async def _create_session(
    client: AsyncClient, project_id: str, external_id: str
) -> dict:
    """Create a session via ``POST /v1/projects/{id}/sessions``."""
    resp = await client.post(
        f"/v1/projects/{project_id}/sessions",
        json={"external_id": external_id},
    )
    assert resp.status_code == 201, f"Session creation failed: {resp.text}"
    body = resp.json()
    UUID(body["id"])
    return body


class TestFullIngestionPipeline:
    """Complete memory ingestion and search flow."""

    # ═════════════════════════════════════════════════════════════════════
    # E2E: create user
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_create_user(
        self,
        admin_client: AsyncClient,
    ) -> None:
        """Create a user via the API and verify the response shape."""
        resp = await admin_client.post(
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
        admin_client: AsyncClient,
        auth_client: AsyncClient,
        org_and_key: dict,
    ) -> None:
        """Create a user and then create a session in the project."""
        # ── Create user (org management — admin JWT) ────────────────────
        user_resp = await admin_client.post(
            "/v1/users",
            json={"external_id": "e2e-test-user-2"},
        )
        assert user_resp.status_code == 201
        UUID(user_resp.json()["id"])
        project_id = org_and_key["project_id"]

        # ── Create session (project scope — API key) ─────────────────────
        session_body = await _create_session(
            auth_client, project_id, "e2e-test-session-2"
        )
        session_id = session_body["id"]
        assert session_body["project_id"] == str(project_id)

        # ── Verify session is listable ───────────────────────────────────
        list_resp = await auth_client.get(f"/v1/projects/{project_id}/sessions")
        assert list_resp.status_code == 200
        list_body = list_resp.json()
        assert session_id in {s["id"] for s in list_body.get("data", [])}

    # ═════════════════════════════════════════════════════════════════════
    # E2E: create user → create session → ingest memory
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_ingest_memory(
        self,
        admin_client: AsyncClient,
        auth_client: AsyncClient,
        org_and_key: dict,
    ) -> None:
        """Ingest messages into a project session — verify 202 accepted."""
        # ── Create user ──────────────────────────────────────────────
        user_resp = await admin_client.post(
            "/v1/users",
            json={"external_id": "e2e-test-user-3"},
        )
        assert user_resp.status_code == 201
        project_id = org_and_key["project_id"]

        # ── Create session ───────────────────────────────────────────
        session_body = await _create_session(
            auth_client, project_id, "e2e-test-session-3"
        )
        session_id = session_body["id"]

        # ── Ingest memory (multipart form, session external ID) ──────
        ingest_resp = await auth_client.post(
            f"/v1/projects/{project_id}/memory",
            data={
                "data": json.dumps(
                    {
                        "session_id": "e2e-test-session-3",
                        "messages": [
                            {"role": "user", "content": "Hello, my name is Alice."},
                            {
                                "role": "assistant",
                                "content": "Nice to meet you, Alice!",
                            },
                        ],
                    }
                )
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
            f"/v1/projects/{project_id}/sessions/{session_id}/messages",
        )
        assert messages_resp.status_code == 200
        messages_body = messages_resp.json()
        # Episodes should be persisted synchronously even though
        # enrichment (embedding, fact extraction) is async.
        assert len(messages_body.get("data", messages_body.get("items", []))) == 2, (
            f"Expected 2 persisted messages, got {messages_body}"
        )

    # ═════════════════════════════════════════════════════════════════════
    # E2E: create user → search (no results expected — no ingested data)
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_search_empty(
        self,
        admin_client: AsyncClient,
        auth_client: AsyncClient,
        org_and_key: dict,
        _search_dispatcher: None,
        _fake_embeddings: None,
    ) -> None:
        """Search with no ingested data — verify 200 with empty results."""
        # ── Create user ──────────────────────────────────────────────
        user_resp = await admin_client.post(
            "/v1/users",
            json={"external_id": "e2e-test-user-4"},
        )
        assert user_resp.status_code == 201
        project_id = org_and_key["project_id"]

        # ── Search (nothing ingested yet) ────────────────────────────
        search_resp = await auth_client.get(
            f"/v1/projects/{project_id}/search",
            params={"query": "Alice", "types": "episodes,facts"},
        )
        assert search_resp.status_code == 200, (
            f"Expected 200, got {search_resp.status_code}: {search_resp.text}"
        )
        body = search_resp.json()
        # Should return valid structure even if empty
        assert isinstance(body, dict)
        assert body.get("results", []) == []

    # ═════════════════════════════════════════════════════════════════════
    # E2E: full flow — create user → session → ingest → list messages
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_full_flow(
        self,
        admin_client: AsyncClient,
        auth_client: AsyncClient,
        org_and_key: dict,
    ) -> None:
        """Complete non-enriched flow: user → session → ingest → verify.

        Background enrichment workers are NOT running, so we only verify
        synchronous persistence (episodes) and API contract compliance.
        """
        # ── 1. Create user ───────────────────────────────────────────
        user_resp = await admin_client.post(
            "/v1/users",
            json={"external_id": "e2e-test-user-5"},
        )
        assert user_resp.status_code == 201, (
            f"User creation failed: {user_resp.status_code} {user_resp.text}"
        )
        user_id: str = user_resp.json()["id"]
        UUID(user_id)
        project_id = org_and_key["project_id"]

        # ── 2. Create session ────────────────────────────────────────
        session_body = await _create_session(
            auth_client, project_id, "e2e-test-session-5"
        )
        session_id: str = session_body["id"]
        UUID(session_id)

        # ── 3. Ingest memory ─────────────────────────────────────────
        ingest_resp = await auth_client.post(
            f"/v1/projects/{project_id}/memory",
            data={
                "data": json.dumps(
                    {
                        "session_id": "e2e-test-session-5",
                        "messages": [
                            {
                                "role": "user",
                                "content": "What is the capital of France?",
                            },
                            {
                                "role": "assistant",
                                "content": "The capital of France is Paris.",
                            },
                        ],
                    }
                )
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
            f"/v1/projects/{project_id}/sessions/{session_id}/messages",
        )
        assert messages_resp.status_code == 200
        messages_body = messages_resp.json()
        messages = messages_body.get("data", messages_body.get("items", []))
        assert len(messages) == 2, f"Expected 2 messages, got {len(messages)}"

        # ── 5. Verify session readback carries the session id ────────
        stats_resp = await auth_client.get(
            f"/v1/projects/{project_id}/sessions/{session_id}",
        )
        assert stats_resp.status_code == 200
        stats_body = stats_resp.json()
        assert stats_body["id"] == session_id
