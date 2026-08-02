"""Unit tests for the memory ingestion router.

Tests ``POST /v1/projects/{project_id}/memory`` (multipart ingestion)
and ``DELETE /v1/projects/{project_id}/memory`` (memory wipe).
"""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock
from uuid import UUID

import orjson
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from pydantic_core import ValidationError as PydanticCoreValidationError
from starlette.requests import Request

from core.exceptions import ConflictError, register_exception_handlers
from dependencies.auth import get_current_user_id
from dependencies.project_auth import require_project_membership
from dependencies.services import get_memory_service
from routers.memory import router
from schemas.memory import IngestMemoryResponse
from services.idempotency_service import IdempotencyService

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


MOCK_MEMORY_SERVICE = AsyncMock()
"""Shared mock instance reused across all tests and DI resolution."""


def _create_app() -> FastAPI:
    """Build a minimal FastAPI app with only the memory router and overridden deps."""
    app = FastAPI()

    # Map the AppError hierarchy (e.g. ConflictError → 409) to RFC 7807
    # bodies, matching the production wiring in services/api/main.py.
    register_exception_handlers(app)

    # model_validate_json() raises pydantic_core.ValidationError — catch it
    # here in the test app so the router returns 422 as documented.
    @app.exception_handler(PydanticCoreValidationError)
    async def _pydantic_core_handler(
        _request: Request,
        exc: PydanticCoreValidationError,
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    app.dependency_overrides[get_memory_service] = lambda: MOCK_MEMORY_SERVICE
    app.dependency_overrides[require_project_membership] = lambda: None
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    app.include_router(router)
    return app


@pytest.fixture(autouse=True)
def _reset_mock() -> None:
    """Reset the shared mock before each test."""
    MOCK_MEMORY_SERVICE.reset_mock()
    MOCK_MEMORY_SERVICE.ingest = AsyncMock()
    MOCK_MEMORY_SERVICE.delete_project_memory = AsyncMock()


@pytest.mark.asyncio
async def test_ingest_messages_success() -> None:
    """POST with valid JSON in the data field and no blobs returns 202."""
    MOCK_MEMORY_SERVICE.ingest.return_value = IngestMemoryResponse(
        job_id="550e8400-e29b-41d4-a716-446655440000",
        episode_count=2,
        blob_count=0,
        status="accepted",
        message="Messages accepted for processing",
    )

    app = _create_app()
    transport = ASGITransport(app=app)
    payload = {
        "session_id": "test-session",
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ],
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/projects/{PROJECT_ID}/memory",
            data={"data": json.dumps(payload)},
            headers={"Idempotency-Key": "test-idempotency-key"},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["episode_count"] == 2
    assert body["blob_count"] == 0
    assert body["job_id"] == "550e8400-e29b-41d4-a716-446655440000"
    assert "Location" in resp.headers
    assert "/memory/jobs/" in resp.headers["Location"]

    MOCK_MEMORY_SERVICE.ingest.assert_awaited_once()
    call_kwargs = MOCK_MEMORY_SERVICE.ingest.await_args[1]
    assert call_kwargs["org_id"] == ORG_ID
    assert call_kwargs["project_id"] == PROJECT_ID
    assert call_kwargs["created_by"] == USER_ID
    assert call_kwargs["session_external_id"] == "test-session"
    assert call_kwargs["idempotency_key"] == "test-idempotency-key"


@pytest.mark.asyncio
async def test_ingest_messages_with_blobs() -> None:
    """POST with valid JSON and a referenced blob file returns 202."""
    MOCK_MEMORY_SERVICE.ingest.return_value = IngestMemoryResponse(
        job_id="550e8400-e29b-41d4-a716-446655440001",
        episode_count=1,
        blob_count=1,
        status="accepted",
        message="Messages accepted for processing",
    )

    app = _create_app()
    transport = ASGITransport(app=app)
    payload = {
        "session_id": "test-session",
        "messages": [
            {
                "role": "user",
                "content": "See attachment",
                "blobs": [{"blob_id": 0, "mime_type": "image/png", "file_name": "test.png"}],
            },
        ],
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/projects/{PROJECT_ID}/memory",
            data={"data": json.dumps(payload)},
            files=[("blobs", ("test.png", b"fake_png_data", "image/png"))],
            headers={"Idempotency-Key": "test-key-2"},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["blob_count"] == 1

    MOCK_MEMORY_SERVICE.ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingest_messages_422_invalid_json() -> None:
    """POST with invalid JSON in the data field returns 422."""
    app = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/projects/{PROJECT_ID}/memory",
            data={"data": "not valid json at all"},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingest_messages_422_missing_messages() -> None:
    """POST with valid JSON but an empty messages list returns 422."""
    app = _create_app()
    transport = ASGITransport(app=app)

    payload = {"session_id": "test", "messages": []}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/projects/{PROJECT_ID}/memory",
            data={"data": json.dumps(payload)},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_project_memory_204() -> None:
    """DELETE returns 204 with no content."""
    app = _create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/v1/projects/{PROJECT_ID}/memory")

    assert resp.status_code == 204
    assert resp.text == ""

    MOCK_MEMORY_SERVICE.delete_project_memory.assert_awaited_once_with(
        org_id=ORG_ID,
        project_id=PROJECT_ID,
    )


@pytest.mark.asyncio
async def test_ingest_messages_conflict_returns_409() -> None:
    """Idempotency-Key reuse with a different body surfaces as RFC 7807 409."""
    MOCK_MEMORY_SERVICE.ingest.side_effect = ConflictError(
        "Idempotency-Key already used with a different request body"
    )

    app = _create_app()
    transport = ASGITransport(app=app)
    payload = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/projects/{PROJECT_ID}/memory",
            data={"data": json.dumps(payload)},
            headers={"Idempotency-Key": "conflict-key"},
        )

    assert resp.status_code == 409
    body = resp.json()
    for field in ("type", "title", "status", "detail"):
        assert field in body, f"RFC 7807 body missing '{field}': {body}"
    assert body["status"] == 409
    assert body["type"].endswith("/conflict")
    assert body["detail"] == (
        "Idempotency-Key already used with a different request body"
    )

    # The service was reached once — the conflict is raised by the service
    # and mapped by the exception handler, not short-circuited in the router.
    MOCK_MEMORY_SERVICE.ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingest_forwards_body_hash() -> None:
    """POST with an Idempotency-Key forwards the canonical body SHA-256."""
    MOCK_MEMORY_SERVICE.ingest.return_value = IngestMemoryResponse(
        job_id="550e8400-e29b-41d4-a716-446655440002",
        episode_count=1,
        blob_count=0,
        status="accepted",
        message="Messages accepted for processing",
    )

    app = _create_app()
    transport = ASGITransport(app=app)
    payload = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "Hash me"}],
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/projects/{PROJECT_ID}/memory",
            data={"data": json.dumps(payload)},
            headers={"Idempotency-Key": "hash-key"},
        )

    assert resp.status_code == 202

    call_kwargs = MOCK_MEMORY_SERVICE.ingest.await_args[1]
    # The router hashes the parsed form payload exactly as the service would.
    expected = IdempotencyService.hash_request_body(
        orjson.loads(json.dumps(payload))
    )
    assert call_kwargs["body_hash"] == expected
    assert re.fullmatch(r"[0-9a-f]{64}", call_kwargs["body_hash"])
    assert call_kwargs["idempotency_key"] == "hash-key"


@pytest.mark.asyncio
async def test_ingest_without_key_forwards_none_body_hash() -> None:
    """POST without an Idempotency-Key forwards ``body_hash=None``.

    routers/memory.py only computes the body hash when an Idempotency-Key
    is present — no key means no hash is forwarded to the service.
    """
    MOCK_MEMORY_SERVICE.ingest.return_value = IngestMemoryResponse(
        job_id="550e8400-e29b-41d4-a716-446655440003",
        episode_count=1,
        blob_count=0,
        status="accepted",
        message="Messages accepted for processing",
    )

    app = _create_app()
    transport = ASGITransport(app=app)
    payload = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "No key"}],
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/projects/{PROJECT_ID}/memory",
            data={"data": json.dumps(payload)},
        )

    assert resp.status_code == 202

    call_kwargs = MOCK_MEMORY_SERVICE.ingest.await_args[1]
    assert call_kwargs["body_hash"] is None
    assert call_kwargs["idempotency_key"] is None


@pytest.mark.asyncio
async def test_ingest_rejects_oversized_idempotency_key() -> None:
    """An Idempotency-Key over 255 chars is rejected with 422 at the boundary.

    The router validates the header length before calling the service, so an
    oversized key is a client error (422), not a 500 propagated from
    IdempotencyService's internal ValueError.
    """
    app = _create_app()
    transport = ASGITransport(app=app)
    payload = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/projects/{PROJECT_ID}/memory",
            data={"data": json.dumps(payload)},
            headers={"Idempotency-Key": "k" * 256},
        )

    assert resp.status_code == 422
    assert "255" in resp.json()["detail"]
    MOCK_MEMORY_SERVICE.ingest.assert_not_called()
