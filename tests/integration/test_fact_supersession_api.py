"""HTTP-level tests for the fact supersession + temporal as-of contract.

Asserts the OBSERVED smoke-test contract against the real app:

    1. POST /v1/projects/{project_id}/facts → 202 {job_id, accepted_count,
       superseded_count, status:"accepted"} — no 409 on conflicts.
    2. Re-posting an IDENTICAL batch → 202, same job_id, superseded_count 0
       (Redis content-dedup replay, 48h TTL).
    3. Same SPO, different content → old fact valid_to set; response
       superseded_count 1.
    4. GET /facts?as_of=<ISO-8601> → 200 PaginatedFactsResponse; facts
       valid at that instant only; valid_from/valid_to/invalid_at present.
    5. GET /facts (no as_of) → current facts only; superseded excluded.
    6. GET /context?query=...&as_of=... → 200, metadata.as_of echoed;
       effective-at content before vs after supersession.
    7. GET /facts?as_of=garbage → 422.
    8. Unauthenticated GET /facts → 401; bogus project_id → 404 (JWT
       auth path — API-key path returns 403, see TestAuth).
    9. /health and /ready at ROOT paths → 200/503 (never 404).
    B1. Same-SPO-different-content batch must NOT hit the content-dedup
       replay (different job_id, superseded_count > 0 on the second call).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

from tests.integration.conftest import asgi_transport

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
async def _wire_graph_backend(isolated_app: Any) -> Any:
    """Attach the graph-backend dispatcher (lifespan normally does this)."""
    from core.graph_backend import init_dispatcher

    isolated_app.state.graph_backend_dispatcher = init_dispatcher()
    return isolated_app


class _FakeEmbedResponse:
    """Fake embed response — mirrors the real backend's shape."""

    def __init__(self, embeddings: list[list[float]] | None = None) -> None:
        self.embeddings = embeddings


class _FakeEmbedBackend:
    async def embed(self, texts, model=None) -> _FakeEmbedResponse:
        return _FakeEmbedResponse(embeddings=[[0.0] * 1536 for _ in texts])


async def _fake_resolve_backend(provider=None, org_config=None) -> _FakeEmbedBackend:
    return _FakeEmbedBackend()


@pytest.fixture(autouse=True)
def _fake_embedding_backend(monkeypatch) -> None:
    """Stub the LLM embedding backend (none configured in the test env)."""
    monkeypatch.setattr("core.llm.resolve_backend", _fake_resolve_backend)


def _fact_payload(
    subject: str, predicate: str, obj: str, content: str | None = None
) -> dict:
    triple: dict[str, str] = {"subject": subject, "predicate": predicate, "object": obj}
    if content is not None:
        triple["content"] = content
    return {"facts": [triple]}


async def _create_user(client: AsyncClient, external_id: str) -> None:
    resp = await client.post("/v1/users", json={"external_id": external_id})
    assert resp.status_code == 201, f"User creation failed: {resp.text}"


class TestIngestContract:
    """OBSERVED contract 1–3 + B1 — POST /facts supersession semantics."""

    async def test_initial_batch_returns_202_shape(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        """Contract 1 — 202 with {job_id, accepted_count, superseded_count, status}."""
        await _create_user(isolated_auth_client, "ss_202_user")
        resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/facts",
            json=_fact_payload("Alice", "likes", "hiking"),
        )
        assert resp.status_code == 202, (
            f"Expected 202, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["accepted_count"] == 1
        assert body["superseded_count"] == 0
        UUID(body["job_id"])

    async def test_identical_batch_replays_same_job_id(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        """Contract 2 — identical re-post → 202, same job_id, superseded_count 0."""
        await _create_user(isolated_auth_client, "ss_dedup_user")
        payload = _fact_payload("DedupEntity", "test", "dedup")

        resp1 = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/facts", json=payload
        )
        assert resp1.status_code == 202
        job_id_1 = resp1.json()["job_id"]
        assert resp1.json()["superseded_count"] == 0

        resp2 = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/facts", json=payload
        )
        assert resp2.status_code == 202
        body2 = resp2.json()
        assert body2["job_id"] == job_id_1, (
            "Redis content-dedup must replay the existing job_id"
        )
        assert body2["superseded_count"] == 0
        assert body2["status"] == "accepted"

    async def test_same_spo_different_content_supersedes(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        """Contract 3 + B1 — same SPO, different content → superseded_count 1,
        and the dedup replay must NOT swallow the second batch."""
        await _create_user(isolated_auth_client, "ss_spo_user")
        first = _fact_payload("Alice", "likes", "hiking", "Alice likes hiking")
        second = _fact_payload(
            "Alice", "likes", "hiking", "Alice absolutely loves hiking"
        )

        resp1 = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/facts", json=first
        )
        assert resp1.status_code == 202
        assert resp1.json()["superseded_count"] == 0

        resp2 = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/facts", json=second
        )
        assert resp2.status_code == 202, (
            f"Expected 202 (no 409), got {resp2.status_code}"
        )
        body2 = resp2.json()
        assert body2["superseded_count"] == 1, (
            "B1: same-SPO-different-content must reach supersession — "
            f"got superseded_count={body2['superseded_count']}"
        )
        assert body2["job_id"] != resp1.json()["job_id"], (
            "B1: different content must NOT replay the dedup job_id"
        )
        assert body2["status"] == "accepted"


class TestFactsListTemporal:
    """OBSERVED contract 4, 5, 7 — GET /facts temporal as-of."""

    async def test_list_current_and_as_of(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        """Contracts 3+4+5 — as-of shows old then new; default shows new."""
        await _create_user(isolated_auth_client, "ss_list_user")
        first = _fact_payload("Alice", "likes", "hiking", "Alice likes hiking")
        second = _fact_payload("Alice", "likes", "hiking", "Alice loves hiking")

        assert (
            await isolated_auth_client.post(
                f"/v1/projects/{isolated_project_id}/facts", json=first
            )
        ).status_code == 202
        old_ts: str | None = None
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/facts"
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1
        old_ts = resp.json()["data"][0]["valid_from"]

        assert (
            await isolated_auth_client.post(
                f"/v1/projects/{isolated_project_id}/facts", json=second
            )
        ).status_code == 202

        # Contract 5 — no as_of → current only; superseded excluded.
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/facts"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body and "next_cursor" in body and "has_more" in body
        assert body["has_more"] is False
        assert len(body["data"]) == 1
        current = body["data"][0]
        assert current["content"] == "Alice loves hiking"
        assert current["valid_to"] is None
        assert current["invalid_at"] is None

        # Contract 4 — as-of inside the ORIGINAL validity window → old content.
        resp_old = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/facts", params={"as_of": old_ts}
        )
        assert resp_old.status_code == 200
        old_data = resp_old.json()["data"]
        assert [f["content"] for f in old_data] == ["Alice likes hiking"], (
            "As-of inside the original window must return the ORIGINAL content"
        )
        # Fields per contract: valid_from/valid_to/invalid_at present.
        assert old_data[0]["valid_to"] is not None  # superseded at T2
        assert old_data[0]["invalid_at"] is None

        # As-of at the NEW fact's valid_from → new content only.
        resp_new = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/facts",
            params={"as_of": current["valid_from"]},
        )
        assert resp_new.status_code == 200
        assert [f["content"] for f in resp_new.json()["data"]] == ["Alice loves hiking"]

    async def test_as_of_garbage_returns_422(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        """Contract 7 — GET /facts?as_of=garbage → 422."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/facts", params={"as_of": "garbage"}
        )
        assert resp.status_code == 422, (
            f"Expected 422, got {resp.status_code}: {resp.text}"
        )
        assert "detail" in resp.json()

    async def test_as_of_naive_timestamp_coerced(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        """Naive ISO-8601 as_of is coerced to UTC-aware (no 500)."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/facts",
            params={"as_of": "2026-01-01T00:00:00"},
        )
        assert resp.status_code == 200


class TestContextAsOf:
    """OBSERVED contract 6 — /context as_of echo + effective-at content."""

    async def test_context_as_of_returns_original_content(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        await _create_user(isolated_auth_client, "ss_ctx_user")
        first = _fact_payload("Alice", "likes", "hiking", "Alice likes hiking")
        second = _fact_payload("Alice", "likes", "hiking", "Alice loves hiking")

        assert (
            await isolated_auth_client.post(
                f"/v1/projects/{isolated_project_id}/facts", json=first
            )
        ).status_code == 202
        old_ts = (
            await isolated_auth_client.get(f"/v1/projects/{isolated_project_id}/facts")
        ).json()["data"][0]["valid_from"]

        assert (
            await isolated_auth_client.post(
                f"/v1/projects/{isolated_project_id}/facts", json=second
            )
        ).status_code == 202
        new_ts = (
            await isolated_auth_client.get(f"/v1/projects/{isolated_project_id}/facts")
        ).json()["data"][0]["valid_from"]

        # ── As-of inside the ORIGINAL window → ORIGINAL content ─────────
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/context",
            params={"query": "hiking", "as_of": old_ts},
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["metadata"]["as_of"] == old_ts, (
            "metadata.as_of must echo the requested effective-at instant"
        )
        assert "Alice likes hiking" in body["context"]
        assert "Alice loves hiking" not in body["context"]

        # ── As-of after supersession → NEW content ───────────────────────
        resp_new = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/context",
            params={"query": "hiking", "as_of": new_ts},
        )
        assert resp_new.status_code == 200
        assert "Alice loves hiking" in resp_new.json()["context"]
        assert "Alice likes hiking" not in resp_new.json()["context"]

        # ── Default (no as_of) → current only ────────────────────────────
        resp_cur = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/context",
            params={"query": "hiking"},
        )
        assert resp_cur.status_code == 200
        assert "Alice loves hiking" in resp_cur.json()["context"]
        assert "Alice likes hiking" not in resp_cur.json()["context"]

    async def test_context_as_of_garbage_returns_422(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/context",
            params={"query": "hiking", "as_of": "not-a-date"},
        )
        assert resp.status_code == 422


class TestAuth:
    """OBSERVED contract 8 — 401 unauthenticated; bogus project 404/403."""

    async def test_unauthenticated_facts_returns_401(self, isolated_app: Any) -> None:
        transport = asgi_transport(isolated_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/v1/projects/00000000-0000-0000-0000-000000000002/facts"
            )
        assert resp.status_code == 401

    async def test_bogus_project_returns_404_via_jwt(
        self, isolated_app: Any, isolated_org_and_key: dict
    ) -> None:
        """JWT auth path: project existence is checked → 404 for a bogus ID.

        The smoke test observed 404 (JWT/dashboard auth).  The API-key
        path (see next test) returns 403 because the key-scope check runs
        first.  Both are asserted so the discrepancy is explicit.
        """

        from core.config import get_settings
        from utils.crypto import create_jwt_token

        token = create_jwt_token(
            data={
                "sub": str(isolated_org_and_key["user_id"]),
                "org_id": str(isolated_org_and_key["org_id"]),
                "role": "member",
                "type": "access",
            },
            secret=get_settings().SECRET_KEY,
            expires_delta=timedelta(minutes=5),
        )
        transport = asgi_transport(isolated_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/v1/projects/00000000-0000-0000-0000-000000000000/facts",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 404, (
            f"JWT auth for a bogus project must 404, got {resp.status_code}"
        )

    async def test_bogus_project_returns_403_via_api_key(
        self, isolated_auth_client: AsyncClient
    ) -> None:
        """API-key auth path: the scope check precedes existence → 403.

        ⚠️ Discrepancy with the smoke contract: the observed 404 is only
        reachable via JWT auth.  With the API-key fixtures the key-scope
        mismatch fires first (matching the pre-existing
        ``test_context_foreign_project_returns_403`` convention).
        """
        resp = await isolated_auth_client.get(
            "/v1/projects/00000000-0000-0000-0000-000000000000/facts"
        )
        assert resp.status_code == 403


class TestRootProbes:
    """OBSERVED contract 9 — /health and /ready at ROOT paths."""

    async def test_health_served_at_root(self, isolated_app: Any) -> None:
        transport = asgi_transport(isolated_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "openzync-api"

    async def test_ready_never_404_at_root(self, isolated_app: Any) -> None:
        """/ready resolves at root — 200 (or 503 degraded, never 404)."""
        isolated_app.state.db_engine = object()
        isolated_app.state.redis = object()
        transport = asgi_transport(isolated_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ready")
        assert resp.status_code in (200, 503), (
            f"/ready must be 200/503, got {resp.status_code}"
        )
