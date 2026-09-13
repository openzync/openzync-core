"""Integration tests for the Admin Schemas CRUD API.

Endpoints under test:

    POST   /v1/admin/schemas   — Create schema (requires admin scope)
    GET    /v1/admin/schemas   — List schemas (requires auth)
    GET    /v1/admin/schemas/{id} — Get single schema
    PUT    /v1/admin/schemas/{id} — Update schema (requires admin scope)
    DELETE /v1/admin/schemas/{id} — Soft-delete schema (requires admin scope)

Covers:
    1. Happy path CRUD cycle
    2. Auth/scope enforcement
    3. Duplicate name rejection
    4. Cross-tenant isolation
    5. Schema type validation
    6. Soft delete

Auth strategy:
    Each test uses the per-test isolation fixtures (``isolated_app`` +
    ``isolated_auth_client``/``admin_client``), so no state leaks between
    tests.  ``require_scope("admin")`` is only satisfiable via JWT auth —
    project-scoped API keys are hard-coded to ``read``/``write`` — so
    mutating CRUD tests use ``admin_client`` (JWT); ``isolated_auth_client``
    (API key) is reserved for the 403-enforcement and GET tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.integration.conftest import asgi_transport, bootstrap_tenant

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _assert_schema_response_shape(body: dict) -> None:
    """Validate that *body* matches ExtractionSchemaResponse shape."""
    assert "id" in body
    assert "organization_id" in body
    assert "name" in body
    assert "type" in body
    assert "json_schema" in body
    assert "is_active" in body
    assert "created_at" in body
    assert "updated_at" in body
    UUID(body["id"])
    UUID(body["organization_id"])


@pytest_asyncio.fixture(loop_scope="function")
async def admin_client(
    isolated_app: Any,
    isolated_org_and_key: dict,
) -> AsyncGenerator[AsyncClient, None]:
    """JWT-authenticated client — admin CRUD is a dashboard (JWT) operation.

    Project-scoped API keys are hard-coded to ``read``/``write`` scopes by
    ``api_key_service``, so ``require_scope("admin")`` can only be satisfied
    by JWT auth.  This mirrors production: schema CRUD happens from the
    dashboard, not from an SDK key.
    """
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {isolated_org_and_key['jwt']}"
        yield client


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdminSchemasCRUD:
    """CRUD tests for the Admin Schemas API."""

    # ═════════════════════════════════════════════════════════════════════════
    # 1. Happy path: create → get → list → update → soft-delete
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_schema_returns_201(
        self,
        admin_client: AsyncClient,
    ) -> None:
        """POST /v1/admin/schemas with valid payload → 201 + schema."""
        response = await admin_client.post(
            "/v1/admin/schemas",
            json={
                "name": "test_intent_labels",
                "type": "classification",
                "json_schema": {
                    "intent": ["greeting", "question", "command"],
                    "emotion": ["joy", "frustration", "neutral"],
                    "valence": ["positive", "negative", "neutral"],
                    "arousal": ["low", "medium", "high"],
                },
            },
        )
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.text}"
        )
        body = response.json()
        _assert_schema_response_shape(body)
        assert body["name"] == "test_intent_labels"
        assert body["type"] == "classification"
        assert body["is_active"] is True

    @pytest.mark.asyncio
    async def test_create_and_get_schema(
        self,
        admin_client: AsyncClient,
    ) -> None:
        """POST → 201, then GET by ID → 200 with same data."""
        create_resp = await admin_client.post(
            "/v1/admin/schemas",
            json={
                "name": "get_test_schema",
                "type": "structured",
                "json_schema": {
                    "type": "object",
                    "properties": {"amount": {"type": "number"}},
                },
            },
        )
        assert create_resp.status_code == 201
        schema_id = create_resp.json()["id"]

        get_resp = await admin_client.get(
            f"/v1/admin/schemas/{schema_id}"
        )
        assert get_resp.status_code == 200
        body = get_resp.json()
        _assert_schema_response_shape(body)
        assert body["id"] == schema_id

    @pytest.mark.asyncio
    async def test_list_schemas(
        self,
        admin_client: AsyncClient,
    ) -> None:
        """GET /v1/admin/schemas — returns list with total."""
        # Create two schemas
        await admin_client.post(
            "/v1/admin/schemas",
            json={
                "name": "list_schema_a",
                "type": "classification",
                "json_schema": {"intent": ["a"]},
            },
        )
        await admin_client.post(
            "/v1/admin/schemas",
            json={
                "name": "list_schema_b",
                "type": "structured",
                "json_schema": {"type": "object"},
            },
        )

        list_resp = await admin_client.get("/v1/admin/schemas")
        assert list_resp.status_code == 200
        body = list_resp.json()
        assert "data" in body
        assert "total" in body
        assert body["total"] >= 2

    @pytest.mark.asyncio
    async def test_list_schemas_filtered_by_type(
        self,
        admin_client: AsyncClient,
    ) -> None:
        """GET /v1/admin/schemas?type=classification — filtered.

        ``GET /v1/admin/schemas`` is gated by
        ``require_permission("configuration:read")`` — the project-scoped
        API key lacks it, so we use the admin JWT client.
        """
        list_resp = await admin_client.get(
            "/v1/admin/schemas",
            params={"type": "classification"},
        )
        assert list_resp.status_code == 200
        body = list_resp.json()
        for schema in body["data"]:
            assert schema["type"] == "classification"

    @pytest.mark.asyncio
    async def test_update_schema(
        self,
        admin_client: AsyncClient,
    ) -> None:
        """PUT /v1/admin/schemas/{id} with new name → 200 + updated."""
        create_resp = await admin_client.post(
            "/v1/admin/schemas",
            json={
                "name": "update_me",
                "type": "classification",
                "json_schema": {"intent": ["hello"]},
            },
        )
        assert create_resp.status_code == 201
        schema_id = create_resp.json()["id"]

        update_resp = await admin_client.put(
            f"/v1/admin/schemas/{schema_id}",
            json={"name": "updated_name"},
        )
        assert update_resp.status_code == 200
        body = update_resp.json()
        assert body["name"] == "updated_name"
        # Type should remain unchanged
        assert body["type"] == "classification"

    @pytest.mark.asyncio
    async def test_soft_delete_schema(
        self,
        admin_client: AsyncClient,
    ) -> None:
        """DELETE /v1/admin/schemas/{id} → 204 + is_active=false."""
        create_resp = await admin_client.post(
            "/v1/admin/schemas",
            json={
                "name": "delete_me",
                "type": "structured",
                "json_schema": {"type": "object"},
            },
        )
        assert create_resp.status_code == 201
        schema_id = create_resp.json()["id"]

        delete_resp = await admin_client.delete(
            f"/v1/admin/schemas/{schema_id}"
        )
        assert delete_resp.status_code == 204

        # Verify soft-deleted
        get_resp = await admin_client.get(
            f"/v1/admin/schemas/{schema_id}"
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["is_active"] is False

    # ═════════════════════════════════════════════════════════════════════════
    # 2. Auth/scope enforcement
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_schema_without_admin_scope_returns_403(
        self,
        isolated_app: Any,
    ) -> None:
        """POST without auth → 401 (no admin scope is never reached)."""
        transport = ASGITransport(app=isolated_app)
        async with AsyncClient(transport=transport, base_url="http://test") as cli:
            response = await cli.post(
                "/v1/admin/schemas",
                json={
                    "name": "no_auth_schema",
                    "type": "structured",
                    "json_schema": {"type": "object"},
                },
            )
        # No auth at all → 401 (not 403: must be authenticated to lack scope)
        assert response.status_code == 401 or response.status_code == 403, (
            f"Expected 401/403, got {response.status_code}: {response.text}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 3. Duplicate name rejection
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_duplicate_schema_name_returns_409(
        self,
        admin_client: AsyncClient,
    ) -> None:
        """POST with existing name → 409 Conflict."""
        name = "dup_schema"
        await admin_client.post(
            "/v1/admin/schemas",
            json={
                "name": name,
                "type": "classification",
                "json_schema": {"intent": ["test"]},
            },
        )
        response = await admin_client.post(
            "/v1/admin/schemas",
            json={
                "name": name,
                "type": "classification",
                "json_schema": {"intent": ["test"]},
            },
        )
        assert response.status_code == 409, (
            f"Expected 409 for duplicate name, "
            f"got {response.status_code}: {response.text}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 4. Schema type validation
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_invalid_classification_schema_returns_422(
        self,
        admin_client: AsyncClient,
    ) -> None:
        """POST with invalid classification json_schema structure → 422."""
        response = await admin_client.post(
            "/v1/admin/schemas",
            json={
                "name": "bad_class_schema",
                "type": "classification",
                "json_schema": {"intent": "not_a_list"},
            },
        )
        assert response.status_code == 422, (
            f"Expected 422 for invalid classification schema, "
            f"got {response.status_code}: {response.text}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 5. Cross-tenant isolation
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_cross_tenant_isolation(
        self,
        isolated_app: Any,
    ) -> None:
        """Schema created by Org A must not be visible to Org B."""
        # Bootstrap Org A and Org B (full tenant flow)
        transport = asgi_transport(isolated_app)
        async with AsyncClient(transport=transport, base_url="http://test") as cli:
            tenant_a = await bootstrap_tenant(isolated_app, cli, "Schema Org A")
            tenant_b = await bootstrap_tenant(isolated_app, cli, "Schema Org B")

        # Org A: create schema (JWT — admin scope is JWT-only)
        async with AsyncClient(transport=transport, base_url="http://test") as cli:
            cli.headers["Authorization"] = f"Bearer {tenant_a['jwt']}"
            create_resp = await cli.post(
                "/v1/admin/schemas",
                json={
                    "name": "org_a_schema",
                    "type": "classification",
                    "json_schema": {"intent": ["a_only"]},
                },
            )
            assert create_resp.status_code == 201
            org_a_schema_id = create_resp.json()["id"]

            # Org A: list schemas — should see 1+
            list_a = await cli.get("/v1/admin/schemas")
            assert list_a.status_code == 200

        # Org B: list schemas — should see 0 (org_a_schema is scoped to Org A)
        async with AsyncClient(transport=transport, base_url="http://test") as cli:
            cli.headers["Authorization"] = f"Bearer {tenant_b['jwt']}"
            list_b = await cli.get("/v1/admin/schemas")
            assert list_b.status_code == 200
            body_b = list_b.json()
            # Org B should NOT see Org A's schema
            ids_b = {s["id"] for s in body_b["data"]}
            assert org_a_schema_id not in ids_b, (
                "Org B should not see Org A's schema"
            )

            # Org B: GET Org A's schema by ID → 404
            get_b = await cli.get(
                f"/v1/admin/schemas/{org_a_schema_id}"
            )
            assert get_b.status_code == 404, (
                "Org B should not be able to GET Org A's schema"
            )
