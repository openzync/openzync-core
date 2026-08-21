"""Unit tests for the facts ingestion router.

Tests ``POST /v1/projects/{project_id}/facts`` — ingest fact triples.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dependencies.auth import get_current_user_id
from dependencies.project_auth import require_project_membership
from dependencies.services import get_fact_service
from routers.facts import router
from schemas.facts import FactBatchResponse

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


MOCK_FACT_SERVICE = AsyncMock()
"""Shared mock instance reused across all tests and DI resolution."""



@pytest.fixture(autouse=True)
def _stub_permission_gate() -> None:
    """Stub the permission gate for every test in this file.

    The router gates with ``require_permission("project:read")`` /
    ``require_permission("project:write")`` — closures created at router
    import time that cannot be keyed in ``dependency_overrides``.  Patching
    ``dependencies.auth._check_permission`` (the shared decision function)
    stubs the gate while keeping the ``require_org_id`` chain intact.  The
    real gate matrix is covered by ``test_admin_gate_matrix.py``.
    """
    with patch("dependencies.auth._check_permission", new=AsyncMock()):
        yield


def _create_app() -> FastAPI:
    """Build a minimal FastAPI app with only the facts router and overridden deps."""
    app = FastAPI()

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    # Shared mock so FastAPI DI and tests reference the same instance.
    from dependencies.db import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_fact_service] = lambda: MOCK_FACT_SERVICE
    app.dependency_overrides[require_project_membership] = lambda: None
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    app.include_router(router)
    return app


@pytest.fixture(autouse=True)
def _reset_mock() -> None:
    """Reset the shared mock before each test."""
    MOCK_FACT_SERVICE.reset_mock()
    MOCK_FACT_SERVICE.ingest_facts = AsyncMock()


@pytest.mark.asyncio
async def test_ingest_facts_success() -> None:
    """POST with a valid FactBatchRequest body returns 202."""
    MOCK_FACT_SERVICE.ingest_facts.return_value = FactBatchResponse(
        job_id="550e8400-e29b-41d4-a716-446655440000",
        accepted_count=2,
        status="accepted",
        message="Facts accepted for processing.",
    )

    app = _create_app()
    transport = ASGITransport(app=app)
    payload = {
        "session_id": "test-session",
        "facts": [
            {"subject": "Alice", "predicate": "likes", "object": "hiking"},
            {"subject": "Bob", "predicate": "works_at", "object": "Acme Corp"},
        ],
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/projects/{PROJECT_ID}/facts",
            json=payload,
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["accepted_count"] == 2
    assert body["job_id"] == "550e8400-e29b-41d4-a716-446655440000"

    # Verify service was called with correct arguments
    MOCK_FACT_SERVICE.ingest_facts.assert_awaited_once()
    call_kwargs = MOCK_FACT_SERVICE.ingest_facts.await_args[1]
    assert call_kwargs["org_id"] == ORG_ID
    assert call_kwargs["project_id"] == PROJECT_ID
    assert call_kwargs["created_by"] == USER_ID
    assert call_kwargs["session_external_id"] == "test-session"
    assert len(call_kwargs["facts"]) == 2


@pytest.mark.asyncio
async def test_ingest_facts_422_empty_body() -> None:
    """POST with an empty JSON body returns 422."""
    app = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/projects/{PROJECT_ID}/facts",
            json={},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingest_facts_422_empty_facts_list() -> None:
    """POST with an empty facts array returns 422."""
    app = _create_app()
    transport = ASGITransport(app=app)

    payload = {"session_id": "test", "facts": []}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/projects/{PROJECT_ID}/facts",
            json=payload,
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingest_facts_422_missing_facts_field() -> None:
    """POST without the required facts field returns 422."""
    app = _create_app()
    transport = ASGITransport(app=app)

    payload = {"session_id": "test"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/projects/{PROJECT_ID}/facts",
            json=payload,
        )

    assert resp.status_code == 422
