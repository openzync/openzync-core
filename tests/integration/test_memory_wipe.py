"""Integration tests for memory wipe endpoint.

Endpoints under test:

    DELETE /v1/projects/{project_id}/memory    — Soft-delete all project memory

Covers:
    1.  Happy path: ingest → wipe → episodes gone (204)
    2.  Idempotent wipe: double DELETE → both return 204
    3.  Wipe on non-existent project → 403 (key scoping)
    4.  No auth → 401
    5.  Wipe preserves sessions (only episodes + facts are soft-deleted)
    6.  Cross-tenant: org B cannot wipe org A's memory
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from tests.integration.conftest import asgi_transport, bootstrap_tenant


@pytest.fixture
async def anon_client(isolated_app: Any) -> AsyncClient:
    """Return an unauthenticated HTTP client — for the 401 test."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestMemoryWipe:
    """Tests for ``DELETE /v1/projects/{project_id}/memory``."""

    # ═════════════════════════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════════════════════════

    @staticmethod
    async def _create_user_and_ingest(
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        external_id: str = "wipe_user",
        session_id: str = "wipe_session",
        message_count: int = 3,
    ) -> str:
        """Create a user, ingest messages, return the user UUID.

        Args:
            isolated_auth_client: Authenticated HTTP client.
            isolated_project_id: Project UUID for scoping memory endpoints.
            external_id: User external identifier.
            session_id: Session external identifier.
            message_count: Number of messages to ingest.

        Returns:
            The created user's UUID string.
        """
        user_resp = await isolated_auth_client.post(
            "/v1/users",
            json={"external_id": external_id},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # Create session before ingesting
        session_resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": session_id},
        )
        assert session_resp.status_code == 201

        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"Message {i}"}
            for i in range(message_count)
        ]

        ingest_resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            data={"data": json.dumps({
                "external_id": external_id,
                "session_id": session_id,
                "messages": messages,
            })},
        )
        assert ingest_resp.status_code == 202, (
            f"Ingestion failed: {ingest_resp.text}"
        )

        return user_id

    @staticmethod
    async def _count_episodes(
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> int:
        """Count total visible episodes across all project sessions.

        Queries the project sessions list and sums the ``message_count``
        from each session's response.

        Args:
            isolated_auth_client: Authenticated HTTP client.
            isolated_project_id: Project UUID for scoping session queries.

        Returns:
            Total message count across all sessions.
        """
        sessions_resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions?limit=100"
        )
        if sessions_resp.status_code != 200:
            return 0
        data = sessions_resp.json().get("data", [])
        return sum(s.get("message_count", 0) for s in data)

    # ═════════════════════════════════════════════════════════════════════════
    # 1.  Happy path — wipe returns 204, episodes gone
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_wipe_memory_returns_204(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """DELETE /v1/projects/{project_id}/memory → 204 + episodes soft-deleted.

        After a successful wipe:
        - The response is 204 No Content.
        - Sessions still exist (only episodes + facts are wiped).
        - Message count across all sessions drops to zero.
        """
        await self._create_user_and_ingest(
            isolated_auth_client,
            isolated_project_id,
            external_id="wipe_happy_user",
            session_id="wipe_happy_session",
            message_count=5,
        )

        # Count episodes before wipe
        before_count = await self._count_episodes(
            isolated_auth_client, isolated_project_id
        )
        assert before_count >= 5, (
            f"Expected at least 5 episodes before wipe, got {before_count}"
        )

        # Wipe
        delete_resp = await isolated_auth_client.delete(
            f"/v1/projects/{isolated_project_id}/memory",
        )
        assert delete_resp.status_code == 204, (
            f"Expected 204, got {delete_resp.status_code}: {delete_resp.text}"
        )

        # Verify wipe — no content in 204 response body
        # Count should be 0 after wipe
        after_count = await self._count_episodes(
            isolated_auth_client, isolated_project_id
        )
        assert after_count == 0, (
            f"Expected 0 episodes after wipe, got {after_count}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 2.  Double wipe is idempotent
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_wipe_twice_is_idempotent(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """Two consecutive DELETE /v1/projects/{project_id}/memory →
        both return 204.

        The second wipe on an already-wiped project must not error — it is
        idempotent by design (soft-delete where condition filters on
        ``is_deleted = false``, so the second update affects 0 rows).
        """
        await self._create_user_and_ingest(
            isolated_auth_client,
            isolated_project_id,
            external_id="wipe_twice_user",
            session_id="wipe_twice_session",
            message_count=3,
        )

        # First wipe
        resp1 = await isolated_auth_client.delete(
            f"/v1/projects/{isolated_project_id}/memory",
        )
        assert resp1.status_code == 204, (
            f"First wipe expected 204, got {resp1.status_code}: {resp1.text}"
        )

        # Second wipe — must also be 204
        resp2 = await isolated_auth_client.delete(
            f"/v1/projects/{isolated_project_id}/memory",
        )
        assert resp2.status_code == 204, (
            f"Second (idempotent) wipe expected 204, "
            f"got {resp2.status_code}: {resp2.text}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 3.  Wipe on non-existent project → 403 (key scoping)
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_wipe_user_not_found_returns_403(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,  # noqa: ARG002 — unused, using fake project UUID
    ) -> None:
        """DELETE /v1/projects/{project_id}/memory with a non-existent project
        UUID → 403.

        ``require_project_membership`` checks the API key's project scope
        before checking if the project exists, so the response is 403,
        not 404.
        """
        fake_project_id = "00000000-0000-0000-0000-000000000000"

        delete_resp = await isolated_auth_client.delete(
            f"/v1/projects/{fake_project_id}/memory",
        )
        # require_project_membership checks the key's project scope
        # before checking if the project exists, returning 403.
        assert delete_resp.status_code == 403, (
            f"Expected 403 for non-existent project (key scoping), "
            f"got {delete_resp.status_code}: {delete_resp.text}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 4.  No auth → 401
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_wipe_no_auth_returns_401(
        self,
        anon_client: AsyncClient,
        isolated_project_id: UUID,  # noqa: ARG002 — fixture presence, unused in path
    ) -> None:
        """DELETE /v1/projects/{project_id}/memory without an ``Authorization``
        header → 401."""
        response = await anon_client.delete(
            "/v1/projects/00000000-0000-0000-0000-000000000000/memory",
        )
        assert response.status_code == 401, (
            f"Expected 401 without auth, "
            f"got {response.status_code}: {response.text}"
        )
        body = response.json()
        # RFC 7807 problem-detail shape
        assert "detail" in body or "status" in body

    # ═════════════════════════════════════════════════════════════════════════
    # 5.  Wipe preserves sessions (only memory is deleted)
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_wipe_preserves_sessions(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """DELETE /v1/projects/{project_id}/memory soft-deletes episodes,
        but sessions remain.

        The memory wipe operation must NOT delete the project's sessions.
        Only the episodes (messages) and facts within them are removed.
        """
        await self._create_user_and_ingest(
            isolated_auth_client,
            isolated_project_id,
            external_id="wipe_preserve_sesh_user",
            session_id="preserve_me",
            message_count=2,
        )

        # Confirm session exists before wipe
        sessions_before = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions?search=preserve_me"
        )
        assert sessions_before.status_code == 200
        data_before = sessions_before.json().get("data", [])
        assert any(s["external_id"] == "preserve_me" for s in data_before), (
            "Session should exist before wipe"
        )

        # Wipe
        delete_resp = await isolated_auth_client.delete(
            f"/v1/projects/{isolated_project_id}/memory",
        )
        assert delete_resp.status_code == 204

        # Session must still exist after wipe
        sessions_after = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions?search=preserve_me"
        )
        assert sessions_after.status_code == 200
        data_after = sessions_after.json().get("data", [])
        assert any(s["external_id"] == "preserve_me" for s in data_after), (
            "Session should still exist after memory wipe"
        )

        # But the session's message_count should now be 0
        matching = [s for s in data_after if s["external_id"] == "preserve_me"]
        assert len(matching) >= 1
        assert matching[0]["message_count"] == 0, (
            f"Expected 0 messages after wipe, "
            f"got {matching[0]['message_count']}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 6.  Cross-tenant: org B cannot wipe org A's memory
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_wipe_cross_tenant(
        self,
        isolated_app: Any,
    ) -> None:
        """Memory ingested by org A must not be wipeable by org B.

        1. Bootstrap org A + org B.
        2. Org A creates a user and ingests memory.
        3. Org B creates a user and session.
        4. Org B tries to DELETE /v1/projects/{project_id}/memory on
           org A's project UUID → 403 (key scoping).
        """
        from dependencies.auth import get_current_user_id

        # ── Bootstrap org A ───────────────────────────────────────────────
        transport_a = ASGITransport(app=isolated_app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport_a, base_url="http://test") as cli:
            tenant_a = await bootstrap_tenant(isolated_app, cli, "Org A")
            project_id_a = tenant_a["project_id"]

        # ── Bootstrap org B + user + session ──────────────────────────────
        transport_b = ASGITransport(app=isolated_app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport_b, base_url="http://test") as cli:
            tenant_b = await bootstrap_tenant(isolated_app, cli, "Org B")

            # Create a user for org B so get_current_user_id has a valid UUID
            cli.headers["Authorization"] = f"Bearer {tenant_b['api_key']}"
            user_resp_b = await cli.post(
                "/v1/users",
                json={"external_id": "cross_tenant_wipe_user_b"},
            )
            assert user_resp_b.status_code == 201
            user_id_b = UUID(user_resp_b.json()["id"])

            # Override get_current_user_id so the session creation works
            isolated_app.dependency_overrides[get_current_user_id] = lambda: user_id_b

            # Create a session for org B to verify its own resources work
            session_resp_b = await cli.post(
                f"/v1/projects/{tenant_b['project_id']}/sessions",
                json={"external_id": "org_b_session"},
            )
            assert session_resp_b.status_code == 201, (
                f"Org B session creation failed: {session_resp_b.text}"
            )

        # ── Org A: create user + ingest memory ────────────────────────────
        async with AsyncClient(
            transport=ASGITransport(app=isolated_app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {tenant_a['api_key']}"
            user_resp = await cli.post(
                "/v1/users",
                json={"external_id": "cross_tenant_wipe_user"},
            )
            assert user_resp.status_code == 201

            # Create session before ingesting
            session_resp = await cli.post(
                f"/v1/projects/{project_id_a}/sessions",
                json={"external_id": "x_wipe_session"},
            )
            assert session_resp.status_code == 201

            ingest_resp = await cli.post(
                f"/v1/projects/{project_id_a}/memory",
                data={"data": json.dumps({
                    "external_id": "cross_tenant_wipe_user",
                    "session_id": "x_wipe_session",
                    "messages": [
                        {"role": "user", "content": "Wipe me if you can"},
                    ],
                })},
            )
            assert ingest_resp.status_code == 202

        # ── Org B: try to wipe Org A's project → 403 ────────────────────
        async with AsyncClient(
            transport=ASGITransport(app=isolated_app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {tenant_b['api_key']}"
            delete_resp = await cli.delete(
                f"/v1/projects/{project_id_a}/memory",
            )

        # Org B's key is scoped to its own project → 403 (key scoping)
        assert delete_resp.status_code == 403, (
            f"Org B should not be able to wipe Org A's memory. "
            f"Got {delete_resp.status_code}: {delete_resp.text}"
        )
