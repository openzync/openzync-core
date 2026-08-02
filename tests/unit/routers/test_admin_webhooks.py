"""Unit tests for the admin webhooks router.

Tests cover all CRUD endpoints under ``/v1/admin/webhooks``:
- ``GET /events`` — public event type listing
- ``GET  /`` — list endpoints
- ``GET /{id}`` — get single + 404
- ``POST /`` — create + 201
- ``PATCH /{id}`` — update + 400 / 404
- ``DELETE /{id}`` — 204 + 404
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from routers.admin_webhooks import router

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
ENDPOINT_ID = UUID("00000000-0000-0000-0000-000000000003")


def _build_app(mock_service: AsyncMock) -> FastAPI:
    """Create a minimal FastAPI app with the webhook router and overridden deps."""
    app = FastAPI()
    app.include_router(router)

    async def _mock_webhook_service() -> AsyncMock:
        return mock_service

    app.dependency_overrides = {}  # reset
    from dependencies.auth import get_dashboard_user, require_org_id
    from dependencies.services import get_webhook_service

    app.dependency_overrides[get_webhook_service] = _mock_webhook_service
    app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
    app.dependency_overrides[get_dashboard_user] = lambda: str(USER_ID)

    @app.middleware("http")
    async def _mock_auth(request: Request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        request.state.api_key_scopes = ["admin", "admin:write"]
        response = await call_next(request)
        return response

    return app


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_service() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
async def client(mock_service: AsyncMock) -> AsyncClient:  # noqa: ANN201
    app = _build_app(mock_service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── GET /events — public ─────────────────────────────────────────────────────


class TestListEventTypes:
    """GET /v1/admin/webhooks/events — public, no auth required."""

    async def test_returns_event_categories(self, client: AsyncClient) -> None:
        """Should return grouped event categories with type/label/description."""
        response = await client.get("/v1/admin/webhooks/events")
        assert response.status_code == 200
        body = response.json()
        assert "data" in body
        # Known categories from core/events.py
        categories = body["data"]
        assert isinstance(categories, dict)
        # Verify at least one known category exists
        assert "Session" in categories
        session_events = categories["Session"]
        assert isinstance(session_events, list)
        assert len(session_events) >= 1
        event = session_events[0]
        assert "type" in event
        assert "label" in event
        assert "category" in event
        assert "description" in event

    async def test_returns_200_without_auth_header(self, client: AsyncClient) -> None:
        """No auth needed — should work without Authorization header."""
        response = await client.get(
            "/v1/admin/webhooks/events",
            headers={},
        )
        assert response.status_code == 200


# ── GET / — list ─────────────────────────────────────────────────────────────


class TestListWebhooks:
    """GET /v1/admin/webhooks — list all endpoints for the org."""

    async def test_returns_endpoints(
        self, client: AsyncClient, mock_service: AsyncMock
    ) -> None:
        """Should return the list of webhook endpoints."""
        mock_service.list_endpoints.return_value = [
            {
                "id": str(ENDPOINT_ID),
                "name": "test-hook",
                "url": "https://example.com/hook",
                "events": ["session.created"],
                "is_active": True,
            }
        ]
        response = await client.get("/v1/admin/webhooks")
        assert response.status_code == 200
        body = response.json()
        assert "data" in body
        assert len(body["data"]) == 1
        assert body["data"][0]["name"] == "test-hook"
        mock_service.list_endpoints.assert_awaited_once_with(ORG_ID)


# ── GET /{endpoint_id} — get single ──────────────────────────────────────────


class TestGetWebhook:
    """GET /v1/admin/webhooks/{endpoint_id} — get single endpoint."""

    async def test_returns_endpoint(
        self, client: AsyncClient, mock_service: AsyncMock
    ) -> None:
        """Should return the requested webhook endpoint."""
        mock_service.get_endpoint.return_value = {
            "id": str(ENDPOINT_ID),
            "name": "my-webhook",
            "url": "https://example.com/hook",
            "events": [],
            "is_active": True,
        }
        response = await client.get(f"/v1/admin/webhooks/{ENDPOINT_ID}")
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["id"] == str(ENDPOINT_ID)
        mock_service.get_endpoint.assert_awaited_once_with(ENDPOINT_ID, ORG_ID)

    async def test_returns_404_when_not_found(
        self, client: AsyncClient, mock_service: AsyncMock
    ) -> None:
        """Should return 404 when the endpoint does not exist."""
        mock_service.get_endpoint.return_value = None
        response = await client.get(f"/v1/admin/webhooks/{uuid4()}")
        assert response.status_code == 404
        detail = response.json().get("detail", "")
        assert "not found" in detail.lower()


# ── POST / — create ──────────────────────────────────────────────────────────


class TestCreateWebhook:
    """POST /v1/admin/webhooks — create a new webhook endpoint."""

    async def test_creates_webhook(
        self, client: AsyncClient, mock_service: AsyncMock
    ) -> None:
        """Should return 201 with the webhook secret response."""
        mock_service.create_endpoint.return_value = (
            {
                "id": str(ENDPOINT_ID),
                "name": "new-hook",
                "url": "https://example.com/hook",
            },
            "whsec_test_secret",
        )
        payload = {
            "name": "new-hook",
            "url": "https://example.com/hook",
            "events": ["session.created"],
        }
        response = await client.post("/v1/admin/webhooks", json=payload)
        assert response.status_code == 201
        body = response.json()
        assert body["id"] == str(ENDPOINT_ID)
        assert body["name"] == "new-hook"
        assert body["secret"] == "whsec_test_secret"
        assert "message" in body
        mock_service.create_endpoint.assert_awaited_once()

    async def test_returns_422_on_invalid_payload(
        self, client: AsyncClient, mock_service: AsyncMock
    ) -> None:
        """Should return 422 when required fields are missing."""
        response = await client.post("/v1/admin/webhooks", json={})
        assert response.status_code == 422

    async def test_returns_422_on_invalid_url(
        self, client: AsyncClient, mock_service: AsyncMock
    ) -> None:
        """Should return 422 when URL is not valid."""
        payload = {"name": "bad-hook", "url": "not-a-url"}
        response = await client.post("/v1/admin/webhooks", json=payload)
        assert response.status_code == 422


# ── PATCH /{endpoint_id} — update ────────────────────────────────────────────


class TestUpdateWebhook:
    """PATCH /v1/admin/webhooks/{endpoint_id} — update an endpoint."""

    async def test_updates_webhook(
        self, client: AsyncClient, mock_service: AsyncMock
    ) -> None:
        """Should return updated webhook data."""
        mock_service.update_endpoint.return_value = {
            "id": str(ENDPOINT_ID),
            "name": "updated-hook",
            "url": "https://example.com/updated",
            "is_active": True,
        }
        payload = {"name": "updated-hook", "is_active": True}
        response = await client.patch(
            f"/v1/admin/webhooks/{ENDPOINT_ID}", json=payload
        )
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["name"] == "updated-hook"
        mock_service.update_endpoint.assert_awaited_once()

    async def test_returns_400_when_no_fields(
        self, client: AsyncClient, mock_service: AsyncMock
    ) -> None:
        """Should return 400 when the update body has no fields."""
        response = await client.patch(
            f"/v1/admin/webhooks/{ENDPOINT_ID}", json={}
        )
        assert response.status_code == 400
        detail = response.json().get("detail", "")
        assert "no fields" in detail.lower()

    async def test_returns_404_when_not_found(
        self, client: AsyncClient, mock_service: AsyncMock
    ) -> None:
        """Should return 404 when the endpoint to update does not exist."""
        mock_service.update_endpoint.return_value = None
        payload = {"name": "ghost-hook"}
        response = await client.patch(
            f"/v1/admin/webhooks/{uuid4()}", json=payload
        )
        assert response.status_code == 404

    async def test_returns_422_on_invalid_payload(
        self, client: AsyncClient, mock_service: AsyncMock
    ) -> None:
        """Should return 422 when field types are wrong."""
        response = await client.patch(
            f"/v1/admin/webhooks/{ENDPOINT_ID}",
            json={"is_active": "not-a-bool"},
        )
        assert response.status_code == 422


# ── DELETE /{endpoint_id} — delete ───────────────────────────────────────────


class TestDeleteWebhook:
    """DELETE /v1/admin/webhooks/{endpoint_id} — delete an endpoint."""

    async def test_deletes_webhook(
        self, client: AsyncClient, mock_service: AsyncMock
    ) -> None:
        """Should return 204 on successful deletion."""
        mock_service.delete_endpoint.return_value = True
        response = await client.delete(f"/v1/admin/webhooks/{ENDPOINT_ID}")
        assert response.status_code == 204
        mock_service.delete_endpoint.assert_awaited_once_with(ENDPOINT_ID, ORG_ID)

    async def test_returns_404_when_not_found(
        self, client: AsyncClient, mock_service: AsyncMock
    ) -> None:
        """Should return 404 when the endpoint to delete does not exist."""
        mock_service.delete_endpoint.return_value = False
        response = await client.delete(f"/v1/admin/webhooks/{uuid4()}")
        assert response.status_code == 404
