"""Integration tests for memory wipe endpoint.

Endpoints under test:

    DELETE /v1/users/{user_id}/memory    — Soft-delete all user memory

Covers:
    1.  Happy path: ingest → wipe → episodes gone (204)
    2.  Idempotent wipe: double DELETE → both return 204
    3.  Wipe on non-existent user → 404
    4.  No auth → 401
    5.  Wipe preserves sessions (only episodes + facts are soft-deleted)
    6.  Cross-tenant: org B cannot wipe org A's memory
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def anon_client(app: pytest.fixture) -> AsyncClient:  # noqa: ARG002
    """Return an unauthenticated HTTP client — for the 401 test."""
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.skip(reason="Needs per-test DB isolation — see TODO")
class TestMemoryWipe:
    """Tests for ``DELETE /v1/users/{user_id}/memory``."""

    # ═════════════════════════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════════════════════════

    @staticmethod
    async def _create_user_and_ingest(
        auth_client: AsyncClient,
        external_id: str = "wipe_user",
        session_id: str = "wipe_session",
        message_count: int = 3,
    ) -> str:
        """Create a user, ingest messages, return the user UUID.

        Args:
            auth_client: Authenticated HTTP client.
            external_id: User external identifier.
            session_id: Session external identifier.
            message_count: Number of messages to ingest.

        Returns:
            The created user's UUID string.
        """
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": external_id},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"Message {i}"}
            for i in range(message_count)
        ]

        ingest_resp = await auth_client.post(
            f"/v1/users/{user_id}/memory",
            json={
                "session_id": session_id,
                "messages": messages,
            },
        )
        assert ingest_resp.status_code == 202, (
            f"Ingestion failed: {ingest_resp.text}"
        )

        return user_id

    @staticmethod
    async def _count_episodes(
        auth_client: AsyncClient, user_id: str
    ) -> int:
        """Count total visible episodes for a user across all sessions.

        Queries the sessions list and sums the ``message_count`` from
        each session's response.

        Args:
            auth_client: Authenticated HTTP client.
            user_id: The user's UUID string.

        Returns:
            Total message count across all sessions.
        """
        sessions_resp = await auth_client.get(
            f"/v1/users/{user_id}/sessions?limit=100"
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
        auth_client: AsyncClient,
    ) -> None:
        """DELETE /memory → 204 + episodes soft-deleted.

        After a successful wipe:
        - The response is 204 No Content.
        - Sessions still exist (only episodes + facts are wiped).
        - Message count across all sessions drops to zero.
        """
        user_id = await self._create_user_and_ingest(
            auth_client,
            external_id="wipe_happy_user",
            session_id="wipe_happy_session",
            message_count=5,
        )

        # Count episodes before wipe
        before_count = await self._count_episodes(auth_client, user_id)
        assert before_count >= 5, (
            f"Expected at least 5 episodes before wipe, got {before_count}"
        )

        # Wipe
        delete_resp = await auth_client.delete(
            f"/v1/users/{user_id}/memory",
        )
        assert delete_resp.status_code == 204, (
            f"Expected 204, got {delete_resp.status_code}: {delete_resp.text}"
        )

        # Verify wipe — no content in 204 response body
        # Count should be 0 after wipe
        after_count = await self._count_episodes(auth_client, user_id)
        assert after_count == 0, (
            f"Expected 0 episodes after wipe, got {after_count}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 2.  Double wipe is idempotent
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_wipe_twice_is_idempotent(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """Two consecutive DELETE /memory calls → both return 204.

        The second wipe on an already-wiped user must not error — it is
        idempotent by design (soft-delete where condition filters on
        ``is_deleted = false``, so the second update affects 0 rows).
        """
        user_id = await self._create_user_and_ingest(
            auth_client,
            external_id="wipe_twice_user",
            session_id="wipe_twice_session",
            message_count=3,
        )

        # First wipe
        resp1 = await auth_client.delete(
            f"/v1/users/{user_id}/memory",
        )
        assert resp1.status_code == 204, (
            f"First wipe expected 204, got {resp1.status_code}: {resp1.text}"
        )

        # Second wipe — must also be 204
        resp2 = await auth_client.delete(
            f"/v1/users/{user_id}/memory",
        )
        assert resp2.status_code == 204, (
            f"Second (idempotent) wipe expected 204, "
            f"got {resp2.status_code}: {resp2.text}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 3.  Wipe on non-existent user → 404
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_wipe_user_not_found_returns_404(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """DELETE /memory with a non-existent user UUID → 404.

        The MemoryService raises NotFoundError when the user does not
        exist, which is mapped to 404 by the global exception handler.
        """
        fake_user_id = "00000000-0000-0000-0000-000000000000"

        delete_resp = await auth_client.delete(
            f"/v1/users/{fake_user_id}/memory",
        )
        assert delete_resp.status_code == 404, (
            f"Expected 404 for non-existent user, "
            f"got {delete_resp.status_code}: {delete_resp.text}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 4.  No auth → 401
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_wipe_no_auth_returns_401(
        self,
        anon_client: AsyncClient,
    ) -> None:
        """DELETE /memory without an ``Authorization`` header → 401."""
        response = await anon_client.delete(
            "/v1/users/00000000-0000-0000-0000-000000000000/memory",
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
        auth_client: AsyncClient,
    ) -> None:
        """DELETE /memory soft-deletes episodes, but sessions remain.

        The memory wipe operation must NOT delete the user's sessions.
        Only the episodes (messages) and facts within them are removed.
        """
        user_id = await self._create_user_and_ingest(
            auth_client,
            external_id="wipe_preserve_sesh_user",
            session_id="preserve_me",
            message_count=2,
        )

        # Confirm session exists before wipe
        sessions_before = await auth_client.get(
            f"/v1/users/{user_id}/sessions?search=preserve_me"
        )
        assert sessions_before.status_code == 200
        data_before = sessions_before.json().get("data", [])
        assert any(s["external_id"] == "preserve_me" for s in data_before), (
            "Session should exist before wipe"
        )

        # Wipe
        delete_resp = await auth_client.delete(
            f"/v1/users/{user_id}/memory",
        )
        assert delete_resp.status_code == 204

        # Session must still exist after wipe
        sessions_after = await auth_client.get(
            f"/v1/users/{user_id}/sessions?search=preserve_me"
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
        app: pytest.fixture,  # noqa: ARG002
    ) -> None:
        """Memory ingested by org A must not be wipeable by org B.

        1. Bootstrap org A + org B.
        2. Org A creates a user and ingests memory.
        3. Org B tries to DELETE /memory on org A's user UUID → 404.
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
                json={"external_id": "cross_tenant_wipe_user"},
            )
            assert user_resp.status_code == 201
            user_id_a = user_resp.json()["id"]

            ingest_resp = await cli.post(
                f"/v1/users/{user_id_a}/memory",
                json={
                    "session_id": "x_wipe_session",
                    "messages": [
                        {"role": "user", "content": "Wipe me if you can"},
                    ],
                },
            )
            assert ingest_resp.status_code == 202

        # ── Org B: try to wipe Org A's user → 404 ─────────────────────────
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {org_b['api_key']}"
            delete_resp = await cli.delete(
                f"/v1/users/{user_id_a}/memory",
            )

        # Org B cannot see Org A's user → 404 (tenant isolation via RLS)
        assert delete_resp.status_code == 404, (
            f"Org B should not be able to wipe Org A's memory. "
            f"Got {delete_resp.status_code}: {delete_resp.text}"
        )
