"""HTTP-level tests for the fact retraction + history contract (Phases 1.2–1.4).

Asserts the observed smoke-test contract against the real app:

    1. POST /v1/projects/{project_id}/facts/{fact_id}/retract → 200 with the
       fact serialized and ``invalid_at`` set.
    2. Re-retracting the same fact → 200 no-op with ``invalid_at`` unchanged
       (idempotent; no second lineage event).
    3. Retracting an unknown fact id → 404 (existence is never leaked).
    4. GET /v1/projects/{project_id}/facts/{fact_id}/history → 200 with the
       fact plus its invalidation-lineage events (newest first).
    5. History for an unknown fact id → 404.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from httpx import AsyncClient

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
    subject: str,
    predicate: str,
    obj: str,
    content: str | None = None,
    session_id: str | None = None,
) -> dict:
    triple: dict[str, str] = {"subject": subject, "predicate": predicate, "object": obj}
    if content is not None:
        triple["content"] = content
    payload: dict = {"facts": [triple]}
    if session_id is not None:
        payload["session_id"] = session_id
    return payload


async def _create_user(client: AsyncClient, external_id: str) -> None:
    resp = await client.post("/v1/users", json={"external_id": external_id})
    assert resp.status_code == 201, f"User creation failed: {resp.text}"


@pytest_asyncio.fixture(loop_scope="function")
async def isolated_fact_session(
    isolated_auth_client: AsyncClient, isolated_project_id: UUID
) -> str:
    """Create a session for fact ingestion and return its external ID."""
    resp = await isolated_auth_client.post(
        f"/v1/projects/{isolated_project_id}/sessions",
        json={"external_id": "session-retract"},
    )
    assert resp.status_code == 201, f"Session creation failed: {resp.text}"
    return "session-retract"


async def _ingest_one_fact(
    client: AsyncClient, project_id: UUID, session_id: str
) -> str:
    """Ingest a single fact and return its id from the list endpoint."""
    resp = await client.post(
        f"/v1/projects/{project_id}/facts",
        json=_fact_payload(
            "Alice", "likes", "hiking", "Alice likes hiking", session_id=session_id
        ),
    )
    assert resp.status_code == 202, f"Ingest failed: {resp.text}"
    listing = await client.get(f"/v1/projects/{project_id}/facts")
    assert listing.status_code == 200
    data = listing.json()["data"]
    assert len(data) == 1, f"Expected one fact, got {len(data)}"
    return data[0]["id"]


class TestRetractEndpoint:
    """POST /facts/{fact_id}/retract — happy path, idempotency, 404."""

    async def test_retract_sets_invalid_at(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_fact_session: str,
    ) -> None:
        """Retracting an open fact returns 200 with ``invalid_at`` set."""
        await _create_user(isolated_auth_client, "retract_user")
        fact_id = await _ingest_one_fact(
            isolated_auth_client, isolated_project_id, isolated_fact_session
        )

        resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/facts/{fact_id}/retract",
            json={"reason": "user correction"},
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["id"] == fact_id
        assert body["invalid_at"] is not None
        assert body["valid_to"] is None, (
            "a hard retraction sets invalid_at, not valid_to"
        )

    async def test_retract_is_idempotent_noop(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_fact_session: str,
    ) -> None:
        """Re-retracting a closed fact is a 200 no-op — invalid_at unchanged."""
        await _create_user(isolated_auth_client, "retract_idem_user")
        fact_id = await _ingest_one_fact(
            isolated_auth_client, isolated_project_id, isolated_fact_session
        )

        first = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/facts/{fact_id}/retract",
            json={"reason": "first"},
        )
        assert first.status_code == 200
        first_invalid_at = first.json()["invalid_at"]
        assert first_invalid_at is not None

        second = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/facts/{fact_id}/retract",
            json={"reason": "second"},
        )
        assert second.status_code == 200
        body = second.json()
        assert body["invalid_at"] == first_invalid_at, (
            "an idempotent retraction must not re-stamp invalid_at"
        )

        # Still exactly one fact — no duplicate rows.
        listing = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/facts"
        )
        assert len(listing.json()["data"]) == 1

    async def test_retract_unknown_fact_returns_404(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_fact_session: str,
    ) -> None:
        """An unknown fact id is a 404 — existence is not leaked."""
        unknown_id = UUID("00000000-0000-0000-0000-00000000dead")

        resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/facts/{unknown_id}/retract",
            json={"reason": "x"},
        )
        assert resp.status_code == 404, (
            f"Expected 404, got {resp.status_code}: {resp.text}"
        )


class TestHistoryEndpoint:
    """GET /facts/{fact_id}/history — fact + lineage events, 404 unknown."""

    async def test_history_returns_fact_and_events(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_fact_session: str,
    ) -> None:
        """After a retraction the history carries the fact and the event."""
        await _create_user(isolated_auth_client, "history_user")
        fact_id = await _ingest_one_fact(
            isolated_auth_client, isolated_project_id, isolated_fact_session
        )
        retract = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/facts/{fact_id}/retract",
            json={"reason": "wrong"},
        )
        assert retract.status_code == 200

        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/facts/{fact_id}/history"
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["fact"]["id"] == fact_id
        assert body["fact"]["invalid_at"] is not None
        assert len(body["events"]) == 1
        event = body["events"][0]
        assert event["kind"] == "retracted"
        assert event["old_fact_id"] == fact_id
        assert event["new_fact_id"] is None
        assert event["reason"] == "wrong"

    async def test_history_unknown_fact_returns_404(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_fact_session: str,
    ) -> None:
        """History for an unknown fact id is a 404."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/facts/{uuid4()}/history"
        )
        assert resp.status_code == 404, (
            f"Expected 404, got {resp.status_code}: {resp.text}"
        )
