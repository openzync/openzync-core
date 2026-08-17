"""Unit tests for the admin schemas router.

Tests cover all CRUD endpoints under ``/v1/admin/schemas``:
- ``POST   /`` — create schema (201 / 422)
- ``GET    /`` — list schemas (filter by type, is_active)
- ``GET    /{id}`` — get single schema
- ``PUT    /{id}`` — update schema
- ``DELETE /{id}`` — soft-delete (204)
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from routers.admin_schemas import router
from schemas.extraction_schemas import UpdateExtractionSchemaRequest

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
SCHEMA_ID = UUID("00000000-0000-0000-0000-000000000003")
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _mock_org_admin_role(monkeypatch) -> None:
    """Resolve the org-admin role check for every test in this file.

    The router gates reads with ``require_permission("configuration:read")``
    and writes with ``require_permission("configuration:write")``; both JWT
    paths funnel into ``core.rbac.get_org_role`` via
    ``dependencies.auth._check_permission``.  Patching the role lookup to
    return ``"admin"`` keeps the full dependency chain exercised (JWT state,
    Redis on app state, DB session) while mocking only the role source of
    truth.  RBAC itself is unit-tested in ``dependencies/test_rbac.py``.
    """

    async def _fake_get_org_role(redis, db, org_id, user_id) -> str:
        return "admin"

    monkeypatch.setattr("dependencies.auth.get_org_role", _fake_get_org_role)


def _stub_schema_response(overrides: dict | None = None) -> dict:
    """Return a canonical extraction schema response dict."""
    base = {
        "id": str(SCHEMA_ID),
        "organization_id": str(ORG_ID),
        "name": "invoice_extraction",
        "type": "structured",
        "json_schema": {"type": "object", "properties": {}},
        "prompt_template": None,
        "is_active": True,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }
    if overrides:
        base.update(overrides)
    return base


def _build_app() -> FastAPI:
    """Create a minimal FastAPI app with the schemas router and overridden deps."""
    app = FastAPI()
    # get_db dependency requires db_session_factory on app.state
    app.state.db_session_factory = MagicMock()
    # _ensure_org_admin requires a Redis client on app.state
    app.state.redis = AsyncMock()
    app.include_router(router)

    from dependencies.auth import require_org_id
    from dependencies.db import get_db

    app.dependency_overrides = {}
    app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
    app.dependency_overrides[get_db] = lambda: AsyncMock(spec=AsyncSession)

    @app.middleware("http")
    async def _mock_auth(request: Request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        request.state.api_key_permissions = []
        response = await call_next(request)
        return response

    return app


@pytest.fixture
def mock_schema_service() -> AsyncMock:
    """Fixture that patches SchemaService for the router module."""
    with patch("routers.admin_schemas.SchemaService") as mock_cls:
        instance = mock_cls.return_value
        instance.create_schema = AsyncMock()
        instance.list_schemas = AsyncMock()
        instance.get_schema = AsyncMock()
        instance.update_schema = AsyncMock()
        instance.delete_schema = AsyncMock()
        yield instance


@pytest.fixture
async def client(mock_schema_service: AsyncMock) -> AsyncClient:  # noqa: ANN201
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── POST / — create schema ───────────────────────────────────────────────────


class TestCreateSchema:
    """POST /v1/admin/schemas — create a new schema (admin scope required)."""

    async def test_creates_schema(
        self,
        client: AsyncClient,
        mock_schema_service: AsyncMock,
    ) -> None:
        """Should return 201 with the created schema."""
        mock_schema_service.create_schema.return_value = _stub_schema_response()

        payload = {
            "name": "invoice_extraction",
            "json_schema": {"type": "object", "properties": {}},
            "type": "structured",
        }
        response = await client.post("/v1/admin/schemas", json=payload)
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "invoice_extraction"
        assert body["type"] == "structured"
        assert body["is_active"] is True
        mock_schema_service.create_schema.assert_awaited_once()

    async def test_returns_422_on_missing_name(
        self, client: AsyncClient, mock_schema_service: AsyncMock
    ) -> None:
        """Should return 422 when the name field is missing."""
        payload = {"json_schema": {"type": "object"}}
        response = await client.post("/v1/admin/schemas", json=payload)
        assert response.status_code == 422

    async def test_returns_422_on_invalid_name_pattern(
        self, client: AsyncClient, mock_schema_service: AsyncMock
    ) -> None:
        """Should return 422 when name starts with a non-letter character."""
        payload = {
            "name": "123_invalid",
            "json_schema": {"type": "object"},
        }
        response = await client.post("/v1/admin/schemas", json=payload)
        assert response.status_code == 422

    async def test_returns_422_on_invalid_type(
        self, client: AsyncClient, mock_schema_service: AsyncMock
    ) -> None:
        """Should return 422 when type is not structured or classification."""
        payload = {
            "name": "test_schema",
            "json_schema": {"type": "object"},
            "type": "invalid",
        }
        response = await client.post("/v1/admin/schemas", json=payload)
        assert response.status_code == 422


# ── GET / — list schemas ─────────────────────────────────────────────────────


class TestListSchemas:
    """GET /v1/admin/schemas — list schemas for the org."""

    async def test_returns_schemas(
        self,
        client: AsyncClient,
        mock_schema_service: AsyncMock,
    ) -> None:
        """Should return a paginated list of schemas."""
        mock_schema_service.list_schemas.return_value = [
            _stub_schema_response(),
            _stub_schema_response(
                {"id": str(uuid4()), "name": "classification_labels", "type": "classification"}
            ),
        ]
        response = await client.get("/v1/admin/schemas")
        assert response.status_code == 200
        body = response.json()
        assert "data" in body
        assert body["total"] == 2
        assert len(body["data"]) == 2

    async def test_filters_by_type(
        self,
        client: AsyncClient,
        mock_schema_service: AsyncMock,
    ) -> None:
        """Should pass the type filter to the service."""
        mock_schema_service.list_schemas.return_value = []
        response = await client.get("/v1/admin/schemas?type=classification")
        assert response.status_code == 200
        mock_schema_service.list_schemas.assert_awaited_once_with(
            org_id=ORG_ID, schema_type="classification", is_active=None
        )

    async def test_filters_by_is_active(
        self,
        client: AsyncClient,
        mock_schema_service: AsyncMock,
    ) -> None:
        """Should pass the is_active filter to the service."""
        mock_schema_service.list_schemas.return_value = []
        response = await client.get("/v1/admin/schemas?is_active=true")
        assert response.status_code == 200
        mock_schema_service.list_schemas.assert_awaited_once_with(
            org_id=ORG_ID, schema_type=None, is_active=True
        )

    async def test_returns_422_on_invalid_type_filter(
        self, client: AsyncClient, mock_schema_service: AsyncMock
    ) -> None:
        """Should return 422 when type filter is invalid."""
        response = await client.get("/v1/admin/schemas?type=invalid")
        assert response.status_code == 422


# ── GET /{schema_id} — get single schema ─────────────────────────────────────


class TestGetSchema:
    """GET /v1/admin/schemas/{schema_id} — get a single schema."""

    async def test_returns_schema(
        self,
        client: AsyncClient,
        mock_schema_service: AsyncMock,
    ) -> None:
        """Should return the requested schema."""
        mock_schema_service.get_schema.return_value = _stub_schema_response()
        response = await client.get(f"/v1/admin/schemas/{SCHEMA_ID}")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(SCHEMA_ID)
        assert body["name"] == "invoice_extraction"
        mock_schema_service.get_schema.assert_awaited_once_with(
            org_id=ORG_ID, schema_id=SCHEMA_ID
        )

    async def test_returns_422_on_invalid_uuid(
        self, client: AsyncClient, mock_schema_service: AsyncMock
    ) -> None:
        """Should return 422 when schema_id is not a valid UUID."""
        response = await client.get("/v1/admin/schemas/not-a-uuid")
        assert response.status_code == 422


# ── PUT /{schema_id} — update schema ─────────────────────────────────────────


class TestUpdateSchema:
    """PUT /v1/admin/schemas/{schema_id} — update a schema (admin scope)."""

    async def test_updates_schema(
        self,
        client: AsyncClient,
        mock_schema_service: AsyncMock,
    ) -> None:
        """Should return the updated schema."""
        mock_schema_service.update_schema.return_value = _stub_schema_response(
            {"name": "updated_schema"}
        )
        payload = {"name": "updated_schema", "is_active": False}
        response = await client.put(
            f"/v1/admin/schemas/{SCHEMA_ID}", json=payload
        )
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "updated_schema"
        mock_schema_service.update_schema.assert_awaited_once_with(
            org_id=ORG_ID,
            schema_id=SCHEMA_ID,
            payload=UpdateExtractionSchemaRequest(**payload),
        )

    async def test_returns_200_on_empty_body(
        self, client: AsyncClient, mock_schema_service: AsyncMock
    ) -> None:
        """Should return 200 — all fields are optional in the schema."""
        mock_schema_service.update_schema.return_value = _stub_schema_response()
        response = await client.put(
            f"/v1/admin/schemas/{SCHEMA_ID}", json={}
        )
        assert response.status_code == 200
        mock_schema_service.update_schema.assert_awaited_once()

    async def test_returns_422_on_invalid_field(
        self, client: AsyncClient, mock_schema_service: AsyncMock
    ) -> None:
        """Should return 422 when a field has an invalid type."""
        response = await client.put(
            f"/v1/admin/schemas/{SCHEMA_ID}",
            json={"is_active": "not-a-bool"},
        )
        assert response.status_code == 422


# ── DELETE /{schema_id} — delete schema ──────────────────────────────────────


class TestDeleteSchema:
    """DELETE /v1/admin/schemas/{schema_id} — soft-delete (admin scope)."""

    async def test_deletes_schema(
        self,
        client: AsyncClient,
        mock_schema_service: AsyncMock,
    ) -> None:
        """Should return 204 on successful soft-delete."""
        mock_schema_service.delete_schema.return_value = None
        response = await client.delete(f"/v1/admin/schemas/{SCHEMA_ID}")
        assert response.status_code == 204
        mock_schema_service.delete_schema.assert_awaited_once_with(
            org_id=ORG_ID, schema_id=SCHEMA_ID
        )

    async def test_returns_422_on_invalid_uuid(
        self, client: AsyncClient, mock_schema_service: AsyncMock
    ) -> None:
        """Should return 422 when schema_id is not a valid UUID."""
        response = await client.delete("/v1/admin/schemas/not-a-uuid")
        assert response.status_code == 422
