"""Unit tests for the audit log router.

Tests the endpoint under ``/v1/admin/audit-logs``:
- ``GET /`` — paginated, filterable audit log listing

The router creates ``AuditLogService(db)`` inline — we patch the service class
to control behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from routers.audit_log import router

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
LOG_ID = UUID("00000000-0000-0000-0000-000000000003")
NOW = datetime.now(timezone.utc)


def _stub_audit_log_entry(
    overrides: dict | None = None,
) -> MagicMock:
    """Return a MagicMock that looks like an AuditLog ORM object."""
    entry = MagicMock()
    entry.id = LOG_ID
    entry.organization_id = ORG_ID
    entry.actor_id = str(USER_ID)
    entry.actor_type = "user"
    entry.action = "session.create"
    entry.resource_type = "session"
    entry.resource_id = str(uuid4())
    entry.details = {"display_name": "Session created", "status_code": 200, "method": "POST", "path": "/v1/sessions"}
    entry.ip_address = "127.0.0.1"
    entry.created_at = NOW
    if overrides:
        for k, v in overrides.items():
            setattr(entry, k, v)
    return entry


def _build_app(mock_service: AsyncMock) -> FastAPI:
    """Create a minimal FastAPI app with the audit-log router."""
    app = FastAPI()
    app.include_router(router)

    from dependencies.auth import get_dashboard_user, require_org_id
    from dependencies.db import get_db

    app.dependency_overrides = {}
    app.dependency_overrides[get_db] = lambda: mock_service._db  # not used directly
    app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
    app.dependency_overrides[get_dashboard_user] = lambda: str(USER_ID)

    @app.middleware("http")
    async def _mock_auth(request: Request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    return app


@pytest.fixture
def mock_audit_service() -> AsyncMock:
    """Patch AuditLogService so the router gets a mock instance."""
    with patch("routers.audit_log.AuditLogService") as mock_cls:
        instance = mock_cls.return_value
        instance.query_logs = AsyncMock()
        yield instance


@pytest.fixture
async def client(mock_audit_service: AsyncMock) -> AsyncClient:  # noqa: ANN201
    app = _build_app(mock_audit_service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── GET / — list audit logs ──────────────────────────────────────────────────


class TestListAuditLogs:
    """GET /v1/admin/audit-logs — paginated audit log listing."""

    async def test_returns_audit_logs(
        self, client: AsyncClient, mock_audit_service: AsyncMock
    ) -> None:
        """Should return paginated audit log entries."""
        entry = _stub_audit_log_entry()
        mock_audit_service.query_logs.return_value = ([entry], 1)

        response = await client.get("/v1/admin/audit-logs")
        assert response.status_code == 200
        body = response.json()

        assert "items" in body
        assert body["total"] == 1
        assert body["limit"] == 50
        assert body["offset"] == 0
        assert len(body["items"]) == 1

        item = body["items"][0]
        assert item["id"] == str(LOG_ID)
        assert item["action"] == "session.create"
        assert item["actor_type"] == "user"
        assert item["display_name"] == "Session created"
        assert item["status_code"] == 200
        assert item["method"] == "POST"
        assert item["path"] == "/v1/sessions"
        assert item["ip_address"] == "127.0.0.1"

        mock_audit_service.query_logs.assert_awaited_once()
        call_kwargs = mock_audit_service.query_logs.await_args.kwargs
        assert call_kwargs["organization_id"] == ORG_ID
        assert call_kwargs["limit"] == 50
        assert call_kwargs["offset"] == 0

    async def test_filters_by_action(
        self, client: AsyncClient, mock_audit_service: AsyncMock
    ) -> None:
        """Should pass the action filter to the service."""
        mock_audit_service.query_logs.return_value = ([], 0)
        response = await client.get(
            "/v1/admin/audit-logs?action=session.create"
        )
        assert response.status_code == 200
        assert mock_audit_service.query_logs.await_args.kwargs["action"] == "session.create"

    async def test_filters_by_actor(
        self, client: AsyncClient, mock_audit_service: AsyncMock
    ) -> None:
        """Should pass actor_id and actor_type filters."""
        mock_audit_service.query_logs.return_value = ([], 0)
        response = await client.get(
            "/v1/admin/audit-logs?actor_id=user-abc&actor_type=user"
        )
        assert response.status_code == 200
        kwargs = mock_audit_service.query_logs.await_args.kwargs
        assert kwargs["actor_id"] == "user-abc"
        assert kwargs["actor_type"] == "user"

    async def test_filters_by_resource(
        self, client: AsyncClient, mock_audit_service: AsyncMock
    ) -> None:
        """Should pass resource_type and resource_id filters."""
        mock_audit_service.query_logs.return_value = ([], 0)
        response = await client.get(
            "/v1/admin/audit-logs?resource_type=session&resource_id=res-123"
        )
        assert response.status_code == 200
        kwargs = mock_audit_service.query_logs.await_args.kwargs
        assert kwargs["resource_type"] == "session"
        assert kwargs["resource_id"] == "res-123"

    async def test_filters_by_status_code(
        self, client: AsyncClient, mock_audit_service: AsyncMock
    ) -> None:
        """Should pass the status_code filter."""
        mock_audit_service.query_logs.return_value = ([], 0)
        response = await client.get("/v1/admin/audit-logs?status_code=200")
        assert response.status_code == 200
        assert mock_audit_service.query_logs.await_args.kwargs["status_code"] == 200

    async def test_filters_by_exclude_prefix(
        self, client: AsyncClient, mock_audit_service: AsyncMock
    ) -> None:
        """Should pass the exclude_prefix filter."""
        mock_audit_service.query_logs.return_value = ([], 0)
        response = await client.get(
            "/v1/admin/audit-logs?exclude_prefix=http.,auth."
        )
        assert response.status_code == 200
        assert mock_audit_service.query_logs.await_args.kwargs["exclude_prefix"] == "http.,auth."

    async def test_filters_by_date_range(
        self, client: AsyncClient, mock_audit_service: AsyncMock
    ) -> None:
        """Should pass created_after and created_before filters."""
        mock_audit_service.query_logs.return_value = ([], 0)
        response = await client.get(
            "/v1/admin/audit-logs?created_after=2026-07-01T00:00:00&created_before=2026-07-31T23:59:59"
        )
        assert response.status_code == 200
        kwargs = mock_audit_service.query_logs.await_args.kwargs
        assert kwargs["created_after"] == "2026-07-01T00:00:00"
        assert kwargs["created_before"] == "2026-07-31T23:59:59"

    async def test_respects_pagination(
        self, client: AsyncClient, mock_audit_service: AsyncMock
    ) -> None:
        """Should pass limit and offset to the service."""
        mock_audit_service.query_logs.return_value = ([], 0)
        response = await client.get(
            "/v1/admin/audit-logs?limit=10&offset=20"
        )
        assert response.status_code == 200
        kwargs = mock_audit_service.query_logs.await_args.kwargs
        assert kwargs["limit"] == 10
        assert kwargs["offset"] == 20

    async def test_returns_422_on_invalid_limit(
        self, client: AsyncClient, mock_audit_service: AsyncMock
    ) -> None:
        """Should return 422 when limit exceeds max or is below min."""
        response = await client.get("/v1/admin/audit-logs?limit=999")
        assert response.status_code == 422

        response = await client.get("/v1/admin/audit-logs?limit=0")
        assert response.status_code == 422

    async def test_returns_422_on_negative_offset(
        self, client: AsyncClient, mock_audit_service: AsyncMock
    ) -> None:
        """Should return 422 when offset is negative."""
        response = await client.get("/v1/admin/audit-logs?offset=-1")
        assert response.status_code == 422

    async def test_returns_empty_list_when_no_entries(
        self, client: AsyncClient, mock_audit_service: AsyncMock
    ) -> None:
        """Should return an empty items list and total 0."""
        mock_audit_service.query_logs.return_value = ([], 0)
        response = await client.get("/v1/admin/audit-logs")
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["total"] == 0

    async def test_handles_null_details(
        self, client: AsyncClient, mock_audit_service: AsyncMock
    ) -> None:
        """Should handle entries with None details gracefully."""
        entry = _stub_audit_log_entry({"details": None})
        mock_audit_service.query_logs.return_value = ([entry], 1)
        response = await client.get("/v1/admin/audit-logs")
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["details"] == {}
        assert item["display_name"] is None
        assert item["status_code"] is None
        assert item["method"] is None
        assert item["path"] is None
