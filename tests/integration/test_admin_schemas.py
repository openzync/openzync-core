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
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

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


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skip(reason="Needs per-test DB isolation — see TODO")
class TestAdminSchemasCRUD:
    """CRUD tests for the Admin Schemas API."""

    # ═════════════════════════════════════════════════════════════════════════
    # 1. Happy path: create → get → list → update → soft-delete
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_schema_returns_201(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /v1/admin/schemas with valid payload → 201 + schema."""
        response = await auth_client.post(
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
        auth_client: AsyncClient,
    ) -> None:
        """POST → 201, then GET by ID → 200 with same data."""
        create_resp = await auth_client.post(
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

        get_resp = await auth_client.get(
            f"/v1/admin/schemas/{schema_id}"
        )
        assert get_resp.status_code == 200
        body = get_resp.json()
        _assert_schema_response_shape(body)
        assert body["id"] == schema_id

    @pytest.mark.asyncio
    async def test_list_schemas(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /v1/admin/schemas — returns list with total."""
        # Create two schemas
        await auth_client.post(
            "/v1/admin/schemas",
            json={
                "name": "list_schema_a",
                "type": "classification",
                "json_schema": {"intent": ["a"]},
            },
        )
        await auth_client.post(
            "/v1/admin/schemas",
            json={
                "name": "list_schema_b",
                "type": "structured",
                "json_schema": {"type": "object"},
            },
        )

        list_resp = await auth_client.get("/v1/admin/schemas")
        assert list_resp.status_code == 200
        body = list_resp.json()
        assert "data" in body
        assert "total" in body
        assert body["total"] >= 2

    @pytest.mark.asyncio
    async def test_list_schemas_filtered_by_type(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /v1/admin/schemas?type=classification — filtered."""
        list_resp = await auth_client.get(
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
        auth_client: AsyncClient,
    ) -> None:
        """PUT /v1/admin/schemas/{id} with new name → 200 + updated."""
        create_resp = await auth_client.post(
            "/v1/admin/schemas",
            json={
                "name": "update_me",
                "type": "classification",
                "json_schema": {"intent": ["hello"]},
            },
        )
        assert create_resp.status_code == 201
        schema_id = create_resp.json()["id"]

        update_resp = await auth_client.put(
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
        auth_client: AsyncClient,
    ) -> None:
        """DELETE /v1/admin/schemas/{id} → 204 + is_active=false."""
        create_resp = await auth_client.post(
            "/v1/admin/schemas",
            json={
                "name": "delete_me",
                "type": "structured",
                "json_schema": {"type": "object"},
            },
        )
        assert create_resp.status_code == 201
        schema_id = create_resp.json()["id"]

        delete_resp = await auth_client.delete(
            f"/v1/admin/schemas/{schema_id}"
        )
        assert delete_resp.status_code == 204

        # Verify soft-deleted
        get_resp = await auth_client.get(
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
        async_client: AsyncClient,  # no auth at all
    ) -> None:
        """POST without admin scope → 403."""
        response = await async_client.post(
            "/v1/admin/schemas",
            json={
                "name": "no_auth_schema",
                "type": "structured",
                "json_schema": {"type": "object"},
            },
        )
        # No auth at all → 401 (not 403 which requires being authenticated but lacking scope)
        assert response.status_code == 401 or response.status_code == 403, (
            f"Expected 401/403, got {response.status_code}: {response.text}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # 3. Duplicate name rejection
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_duplicate_schema_name_returns_409(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST with existing name → 409 Conflict."""
        name = "dup_schema"
        await auth_client.post(
            "/v1/admin/schemas",
            json={
                "name": name,
                "type": "classification",
                "json_schema": {"intent": ["test"]},
            },
        )
        response = await auth_client.post(
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
        auth_client: AsyncClient,
    ) -> None:
        """POST with invalid classification json_schema structure → 422."""
        response = await auth_client.post(
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
        app: Any,
    ) -> None:
        """Schema created by Org A must not be visible to Org B."""
        # Bootstrap Org A and Org B
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as cli:
            resp_a = await cli.post(
                "/admin/organizations",
                json={"name": "Schema Org A", "plan": "free"},
            )
            assert resp_a.status_code == 201
            org_a = resp_a.json()

            resp_b = await cli.post(
                "/admin/organizations",
                json={"name": "Schema Org B", "plan": "free"},
            )
            assert resp_b.status_code == 201
            org_b = resp_b.json()

        # Org A: create schema
        async with AsyncClient(transport=transport, base_url="http://test") as cli:
            cli.headers["Authorization"] = f"Bearer {org_a['api_key']}"
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
            cli.headers["Authorization"] = f"Bearer {org_b['api_key']}"
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
