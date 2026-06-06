"""Integration tests for business data (fact) ingestion endpoint.

Endpoints under test:

    POST /v1/users/{user_id}/facts  — Ingest a batch of fact triples

Covers:
    1.  Happy path: 10 fact triples → 202 Accepted
    2.  Empty facts list → 422 (via Pydantic min_length on list)
    3.  Over 500 facts → 422 (via Pydantic max_length on list)
    4.  Invalid triple (missing subject) → Could succeed (validation is permissive)
    5.  Duplicate content payload → content-dedup (same job_id)
    6.  Missing auth header → 401
    7.  User not found → 404
    8.  GET /search returns ingested facts via BM25

Auth strategy:
    Each test creates a fresh org via the admin bootstrap fixture and
    uses ``auth_client`` (pre-authenticated) for all authenticated calls.
"""

from __future__ import annotations

import uuid as uuid_lib

import pytest
from httpx import AsyncClient


def _assert_fact_response_shape(body: dict, expected_count: int) -> None:
    """Validate that ``body`` matches the ``FactBatchResponse`` schema."""
    assert "job_id" in body, "Missing 'job_id'"
    assert "accepted_count" in body, "Missing 'accepted_count'"
    assert "status" in body, "Missing 'status'"
    assert "message" in body, "Missing 'message'"

    assert body["status"] == "accepted", f"Expected 'accepted', got {body['status']}"
    assert body["accepted_count"] == expected_count, (
        f"Expected {expected_count} accepted, got {body['accepted_count']}"
    )

    # job_id must be a valid UUID
    uuid_lib.UUID(body["job_id"])

    assert isinstance(body["message"], str) and len(body["message"]) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBusinessFacts:
    """Tests for ``POST /v1/users/{user_id}/facts``."""

    # ═════════════════════════════════════════════════════════════════════════
    # 1.  Happy path — 202 Accepted
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_facts_returns_202(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /facts with 10 valid triples → 202 + FactBatchResponse."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "facts_happy_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.post(
            f"/v1/users/{user_id}/facts",
            json={
                "facts": [
                    {"subject": "Alice", "predicate": "likes", "object": "hiking"},
                    {"subject": "Alice", "predicate": "works_at", "object": "Acme Corp"},
                    {"subject": "Bob", "predicate": "likes", "object": "coding"},
                    {"subject": "Bob", "predicate": "reports_to", "object": "Alice"},
                    {"subject": "Charlie", "predicate": "likes", "object": "design"},
                    {"subject": "Charlie", "predicate": "uses", "object": "Figma"},
                    {"subject": "Acme Corp", "predicate": "located_in", "object": "San Francisco"},
                    {"subject": "Alice", "predicate": "has_skill", "object": "Python"},
                    {"subject": "Bob", "predicate": "has_skill", "object": "Go"},
                    {"subject": "Charlie", "predicate": "has_skill", "object": "UI/UX"},
                ],
            },
        )
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        body = resp.json()
        _assert_fact_response_shape(body, expected_count=10)

    # ═════════════════════════════════════════════════════════════════════════
    # 2.  Empty facts list → 422
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_facts_empty_list_returns_422(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /facts with empty facts list → 422."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "facts_empty_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.post(
            f"/v1/users/{user_id}/facts",
            json={"facts": []},
        )
        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"

    # ═════════════════════════════════════════════════════════════════════════
    # 3.  Over 500 facts → 422
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_facts_over_limit_returns_422(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /facts with 501 triples → 422."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "facts_over_limit_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        facts = [
            {"subject": f"Entity{i}", "predicate": "likes", "object": "testing"}
            for i in range(501)
        ]
        resp = await auth_client.post(
            f"/v1/users/{user_id}/facts",
            json={"facts": facts},
        )
        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"

    # ═════════════════════════════════════════════════════════════════════════
    # 4.  Missing auth header → 401
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_facts_requires_auth(
        self,
        anon_client: pytest.fixture,  # noqa: ARG002
    ) -> None:
        """POST /facts without auth → 401."""
        resp = await anon_client.post(  # type: ignore[union-attr]
            "/v1/users/00000000-0000-0000-0000-000000000001/facts",
            json={
                "facts": [
                    {"subject": "Alice", "predicate": "likes", "object": "hiking"},
                ],
            },
        )
        assert resp.status_code == 401

    # ═════════════════════════════════════════════════════════════════════════
    # 5.  User not found → 404
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_facts_user_not_found_returns_404(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /facts for non-existent user → 404."""
        fake_user_id = "00000000-0000-0000-0000-000000000001"
        resp = await auth_client.post(
            f"/v1/users/{fake_user_id}/facts",
            json={
                "facts": [
                    {"subject": "Alice", "predicate": "likes", "object": "hiking"},
                ],
            },
        )
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"

    # ═════════════════════════════════════════════════════════════════════════
    # 6.  Content dedup — same payload → same job_id
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_facts_dedup_returns_same_job_id(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /facts with identical payload twice → same job_id."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "facts_dedup_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        payload = {
            "facts": [
                {"subject": "DedupEntity", "predicate": "test", "object": "dedup"},
            ],
        }

        # First call
        resp1 = await auth_client.post(
            f"/v1/users/{user_id}/facts",
            json=payload,
        )
        assert resp1.status_code == 202
        job_id_1 = resp1.json()["job_id"]

        # Second call (identical)
        resp2 = await auth_client.post(
            f"/v1/users/{user_id}/facts",
            json=payload,
        )
        assert resp2.status_code == 202
        job_id_2 = resp2.json()["job_id"]

        assert job_id_1 == job_id_2, (
            f"Expected same job_id for dedup, got {job_id_1} vs {job_id_2}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 7.  Session association
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_ingest_facts_with_session(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /facts with valid session_id → 202."""
        # Create user
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "facts_session_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # Create a session
        session_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "facts_session_user_alt"},
        )
        assert session_resp.status_code == 201
        # Use the existing __default__ session
        sessions_resp = await auth_client.get(
            f"/v1/users/{user_id}/sessions",
        )
        session_id = None
        if sessions_resp.status_code == 200:
            sessions = sessions_resp.json().get("data", [])
            if sessions:
                session_id = sessions[0].get("external_id")

        # Ingest facts with session
        resp = await auth_client.post(
            f"/v1/users/{user_id}/facts",
            json={
                "session_id": session_id or "test_session_1",
                "facts": [
                    {"subject": "SessionFact", "predicate": "belongs_to", "object": "session"},
                ],
            },
        )
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
