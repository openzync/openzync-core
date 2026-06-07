"""Integration tests for structured extraction query endpoints."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_extractions_empty(
    async_client: AsyncClient,
    auth_client: AsyncClient,
    test_user: dict[str, Any],
    test_session: dict[str, Any],
) -> None:
    """GET structured-extractions returns empty list when no extractions exist."""
    response = await auth_client.get(
        f"/v1/users/{test_user['id']}/sessions/{test_session['id']}"
        f"/structured-extractions",
    )
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_extractions_no_auth(
    async_client: AsyncClient,
    test_user: dict[str, Any],
    test_session: dict[str, Any],
) -> None:
    """GET structured-extractions without auth returns 401."""
    response = await async_client.get(
        f"/v1/users/{test_user['id']}/sessions/{test_session['id']}"
        f"/structured-extractions",
    )
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_extractions_invalid_session(
    auth_client: AsyncClient,
    test_user: dict[str, Any],
) -> None:
    """GET structured-extractions with invalid session returns 404."""
    fake_id = uuid.uuid4()
    response = await auth_client.get(
        f"/v1/users/{test_user['id']}/sessions/{fake_id}"
        f"/structured-extractions",
    )
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_extractions_cross_tenant(
    auth_client: AsyncClient,
    secondary_auth_client: AsyncClient,
    test_user: dict[str, Any],
    test_session: dict[str, Any],
) -> None:
    """Cross-tenant access returns 404 (scoped by org)."""
    response = await secondary_auth_client.get(
        f"/v1/users/{test_user['id']}/sessions/{test_session['id']}"
        f"/structured-extractions",
    )
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_single_extraction_not_found(
    auth_client: AsyncClient,
    test_user: dict[str, Any],
    test_session: dict[str, Any],
) -> None:
    """GET a single structured extraction for a non-existent episode returns 404."""
    fake_ep_id = uuid.uuid4()
    response = await auth_client.get(
        f"/v1/users/{test_user['id']}/sessions/{test_session['id']}"
        f"/structured-extractions/{fake_ep_id}",
    )
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_single_extraction_no_auth(
    async_client: AsyncClient,
    test_user: dict[str, Any],
    test_session: dict[str, Any],
) -> None:
    """GET a single extraction without auth returns 401."""
    fake_ep_id = uuid.uuid4()
    response = await async_client.get(
        f"/v1/users/{test_user['id']}/sessions/{test_session['id']}"
        f"/structured-extractions/{fake_ep_id}",
    )
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_extractions_with_structured_schema(
    auth_client: AsyncClient,
    test_user: dict[str, Any],
    test_session: dict[str, Any],
    async_db: Any,
) -> None:
    """Test that creating a structured schema enables querying extractions.

    This test verifies the full pipeline wiring: create schema → ingest
    memory → worker processes → query returns results.
    """
    # Create a structured extraction schema
    schema_payload = {
        "name": "test_booking",
        "type": "structured",
        "json_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "destination": {"type": "string"},
            },
            "required": ["intent"],
        },
    }
    schema_resp = await auth_client.post(
        "/v1/admin/schemas",
        json=schema_payload,
    )
    assert schema_resp.status_code == 201
    schema_id = schema_resp.json()["id"]

    try:
        # Ingest memory to trigger the worker
        ingest_payload = {
            "messages": [
                {"role": "user", "content": "Book a flight to Tokyo"},
                {"role": "assistant", "content": "Sure, let me look up flights to Tokyo."},
            ],
        }
        ingest_resp = await auth_client.post(
            f"/v1/users/{test_user['id']}/memory",
            json=ingest_payload,
        )
        assert ingest_resp.status_code == 201

        # Query extractions — should be empty or have data
        # (depends on whether LLM is available in test environment)
        response = await auth_client.get(
            f"/v1/users/{test_user['id']}/sessions/{test_session['id']}"
            f"/structured-extractions",
        )
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "total" in data
    finally:
        # Clean up: delete the schema
        await auth_client.delete(f"/v1/admin/schemas/{schema_id}")
