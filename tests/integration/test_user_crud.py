"""Integration tests for User CRUD endpoints.

Endpoints under test (all under ``/v1/users``):

    POST   /v1/users              — Create a user
    GET    /v1/users              — List users (cursor pagination, search)
    GET    /v1/users/{user_id}    — Get a single user with stats
    PATCH  /v1/users/{user_id}    — Update user fields
    DELETE /v1/users/{user_id}    — Soft-delete a user

Auth strategy:
    Each test class creates a fresh organization via ``org_and_key``
    (admin bootstrap) and uses the returned API key for authentication.
    This ensures tenant isolation and no cross-test pollution.

Test cases (11):
    1.  ``test_create_user``                     — 201 + UserResponse shape
    2.  ``test_create_duplicate_external_id``     — same external_id → 409
    3.  ``test_get_user``                        — 200 + UserResponseWithStats
    4.  ``test_get_user_not_found``               — non-existent UUID → 404
    5.  ``test_update_user``                     — PATCH partial fields → 200
    6.  ``test_update_user_metadata_merge``       — PATCH merges, not replaces
    7.  ``test_delete_user``                     — 204 + subsequent GET → 404
    8.  ``test_list_users_paginated``             — cursor pagination
    9.  ``test_list_users_search``                — search filter
    10. ``test_cross_tenant_isolation``           — org B cannot read org A's user
    11. ``test_no_auth_returns_401``              — missing auth header → 401
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient


class TestUserCrud:
    """Full CRUD lifecycle for the ``/v1/users`` endpoint family.

    Each test starts by creating a fresh organization and user so tests
    are fully self-contained and independent.
    """

    # ═════════════════════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════════════════════

    @pytest.fixture
    async def anon_client(self, app: pytest.fixture) -> AsyncClient:  # noqa: ARG002
        """Unauthenticated HTTP client — for the 401 test."""
        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    @staticmethod
    def _assert_user_response_shape(body: dict) -> None:
        """Validate that ``body`` matches the ``UserResponse`` schema.

        ``UserResponse`` fields: ``id``, ``external_id``, ``name``, ``email``,
        ``metadata``, ``organization_id``, ``created_at``, ``updated_at``,
        ``is_deleted``.
        """
        assert "id" in body, "Missing 'id'"
        assert "external_id" in body, "Missing 'external_id'"
        assert "organization_id" in body, "Missing 'organization_id'"
        assert "created_at" in body, "Missing 'created_at'"
        assert "updated_at" in body, "Missing 'updated_at'"
        assert "metadata" in body, "Missing 'metadata'"
        assert "is_deleted" in body, "Missing 'is_deleted'"

        # Validate UUIDs
        UUID(body["id"])
        UUID(body["organization_id"])

        # Defaults
        assert body["is_deleted"] is False
        assert isinstance(body["metadata"], dict)

    # ═════════════════════════════════════════════════════════════════════
    # 1.  Create user — happy path
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_user(self, auth_client: AsyncClient) -> None:
        """POST /v1/users → 201 with a valid UserResponse.

        All optional fields should be reflected in the response.
        """
        response = await auth_client.post(
            "/v1/users",
            json={
                "external_id": "user_001",
                "name": "Alice Johnson",
                "email": "alice@example.com",
                "metadata": {"plan": "pro", "region": "us-east"},
            },
        )
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.text}"
        )
        body = response.json()

        self._assert_user_response_shape(body)
        assert body["external_id"] == "user_001"
        assert body["name"] == "Alice Johnson"
        assert body["email"] == "alice@example.com"
        assert body["metadata"] == {"plan": "pro", "region": "us-east"}

    # ═════════════════════════════════════════════════════════════════════
    # 2.  Duplicate external_id → 409
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_duplicate_external_id(
        self, auth_client: AsyncClient
    ) -> None:
        """POST /v1/users with the same external_id → 409 Conflict.

        The ``(organization_id, external_id)`` unique constraint should
        prevent duplicate user creation.
        """
        # Create the first user
        resp1 = await auth_client.post(
            "/v1/users",
            json={"external_id": "dup_user"},
        )
        assert resp1.status_code == 201, f"First creation failed: {resp1.text}"

        # Attempt to create a second user with the same external_id
        response = await auth_client.post(
            "/v1/users",
            json={"external_id": "dup_user"},
        )
        assert response.status_code == 409, (
            f"Expected 409 for duplicate external_id, "
            f"got {response.status_code}: {response.text}"
        )
        body = response.json()
        # ⚠️ Error body should follow RFC 7807 — look for type/status/code
        assert "status" in body or "detail" in body, (
            "Expected RFC 7807 problem-detail body"
        )

    # ═════════════════════════════════════════════════════════════════════
    # 3.  Get user → 200
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_get_user(self, auth_client: AsyncClient) -> None:
        """GET /v1/users/{id} → 200 with UserResponseWithStats.

        ``UserResponseWithStats`` extends ``UserResponse`` with:
        ``message_count``, ``fact_count``, ``session_count``.
        """
        # Seed a user
        created = await auth_client.post(
            "/v1/users",
            json={"external_id": "get_test", "name": "Bob"},
        )
        assert created.status_code == 201
        user_id = created.json()["id"]

        # Fetch the user
        response = await auth_client.get(f"/v1/users/{user_id}")
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        body = response.json()

        self._assert_user_response_shape(body)
        assert body["id"] == user_id
        assert body["external_id"] == "get_test"
        assert body["name"] == "Bob"

        # Stats fields (should be zero for a fresh user)
        assert "message_count" in body, "Missing message_count"
        assert "fact_count" in body, "Missing fact_count"
        assert "session_count" in body, "Missing session_count"
        assert body["message_count"] == 0
        assert body["fact_count"] == 0
        assert body["session_count"] == 0

    # ═════════════════════════════════════════════════════════════════════
    # 4.  Get user — not found → 404
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_get_user_not_found(self, auth_client: AsyncClient) -> None:
        """GET /v1/users with a non-existent UUID → 404."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = await auth_client.get(f"/v1/users/{fake_id}")
        assert response.status_code == 404, (
            f"Expected 404 for non-existent user, "
            f"got {response.status_code}: {response.text}"
        )

    # ═════════════════════════════════════════════════════════════════════
    # 5.  Update user — partial fields → 200
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_update_user(self, auth_client: AsyncClient) -> None:
        """PATCH /v1/users/{id} with partial data → 200, fields updated.

        Only the provided fields should change; unprovided fields
        should remain at their previous values.
        """
        # Create a user
        created = await auth_client.post(
            "/v1/users",
            json={
                "external_id": "update_test",
                "name": "Original Name",
                "email": "original@example.com",
            },
        )
        assert created.status_code == 201
        user_id = created.json()["id"]

        # Patch only the name
        response = await auth_client.patch(
            f"/v1/users/{user_id}",
            json={"name": "Updated Name"},
        )
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        body = response.json()
        assert body["name"] == "Updated Name"
        # Email should be unchanged
        assert body["email"] == "original@example.com"

    # ═════════════════════════════════════════════════════════════════════
    # 6.  Update user — metadata merge semantics
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_update_user_metadata_merge(
        self, auth_client: AsyncClient
    ) -> None:
        """PATCH metadata is deep-merged, not replaced.

        - Existing keys that are not in the PATCH body must be preserved.
        - Keys in the PATCH body override existing values.
        - New keys in the PATCH body are added.
        """
        # Create user with initial metadata
        created = await auth_client.post(
            "/v1/users",
            json={
                "external_id": "merge_test",
                "metadata": {
                    "plan": "pro",
                    "region": "us-east",
                    "nested": {"a": 1, "b": 2},
                },
            },
        )
        assert created.status_code == 201
        user_id = created.json()["id"]

        # Patch with new metadata
        patch_resp = await auth_client.patch(
            f"/v1/users/{user_id}",
            json={
                "metadata": {
                    "region": "eu-west",  # override
                    "tier": "gold",       # new key
                    "nested": {"a": 99},  # partial override of nested dict
                }
            },
        )
        assert patch_resp.status_code == 200, (
            f"PATCH failed: {patch_resp.text}"
        )

        # Fetch and verify merge semantics
        get_resp = await auth_client.get(f"/v1/users/{user_id}")
        assert get_resp.status_code == 200
        metadata = get_resp.json()["metadata"]

        # "plan" was not in the PATCH body → preserved
        assert metadata["plan"] == "pro", (
            f"Expected 'pro', got {metadata.get('plan')}"
        )
        # "region" was overridden
        assert metadata["region"] == "eu-west"
        # "tier" was added
        assert metadata["tier"] == "gold"
        # "nested" should be merged (existing key 'b' preserved, 'a' overridden)
        assert metadata["nested"]["a"] == 99
        assert metadata["nested"]["b"] == 2, (
            "Nested key 'b' should be preserved during merge"
        )

    # ═════════════════════════════════════════════════════════════════════
    # 7.  Delete user → 204
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_delete_user(self, auth_client: AsyncClient) -> None:
        """DELETE /v1/users/{id} → 204, subsequent GET → 404.

        Verify the soft-delete lifecycle:
        - DELETE returns 204 No Content.
        - Immediately fetching the same user returns 404 (soft-deleted).
        """
        # Create user
        created = await auth_client.post(
            "/v1/users",
            json={"external_id": "del_test"},
        )
        assert created.status_code == 201
        user_id = created.json()["id"]

        # Delete
        delete_resp = await auth_client.delete(f"/v1/users/{user_id}")
        assert delete_resp.status_code == 204, (
            f"Expected 204, got {delete_resp.status_code}: {delete_resp.text}"
        )

        # Verify it's gone
        get_resp = await auth_client.get(f"/v1/users/{user_id}")
        assert get_resp.status_code == 404, (
            f"Expected 404 after delete, got {get_resp.status_code}: {get_resp.text}"
        )

    # ═════════════════════════════════════════════════════════════════════
    # 8.  List users — cursor pagination
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_list_users_paginated(self, auth_client: AsyncClient) -> None:
        """GET /v1/users with cursor-based pagination.

        Create 5 users, fetch with limit=2:
        - Page 1: 2 items, ``has_more=True``, ``next_cursor`` is not null.
        - Page 2: 2 items, ``has_more=True``.
        - Page 3: 1 item,  ``has_more=False``, ``next_cursor`` is null.
        """
        # Seed 5 users
        for i in range(5):
            resp = await auth_client.post(
                "/v1/users",
                json={"external_id": f"paginate_user_{i}"},
            )
            assert resp.status_code == 201, f"Seed failed at index {i}"

        # Page 1: limit=2
        page1 = await auth_client.get("/v1/users?limit=2")
        assert page1.status_code == 200
        body1 = page1.json()

        assert "data" in body1, "Missing 'data' in list response"
        assert "next_cursor" in body1, "Missing 'next_cursor'"
        assert "has_more" in body1, "Missing 'has_more'"
        assert len(body1["data"]) == 2, f"Expected 2 items, got {len(body1['data'])}"
        assert body1["has_more"] is True
        assert body1["next_cursor"] is not None, (
            "Expected non-null cursor for page 1"
        )

        # Page 2: follow cursor
        page2 = await auth_client.get(
            f"/v1/users?limit=2&cursor={body1['next_cursor']}"
        )
        assert page2.status_code == 200
        body2 = page2.json()
        assert len(body2["data"]) == 2
        assert body2["has_more"] is True
        assert body2["next_cursor"] is not None

        # Page 3: final page
        page3 = await auth_client.get(
            f"/v1/users?limit=2&cursor={body2['next_cursor']}"
        )
        assert page3.status_code == 200
        body3 = page3.json()
        assert len(body3["data"]) == 1
        assert body3["has_more"] is False
        assert body3["next_cursor"] is None

    # ═════════════════════════════════════════════════════════════════════
    # 9.  List users — search filter
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_list_users_search(self, auth_client: AsyncClient) -> None:
        """GET /v1/users?search=alice filters by external_id, name, or email.

        The search should match against multiple fields via ILIKE.
        """
        # Seed users with distinct identifiers
        await auth_client.post(
            "/v1/users",
            json={
                "external_id": "alice_smith",
                "name": "Alice Smith",
                "email": "alice@test.com",
            },
        )
        await auth_client.post(
            "/v1/users",
            json={
                "external_id": "bob_jones",
                "name": "Bob Jones",
                "email": "bob@test.com",
            },
        )

        # Search for "alice" — should match by external_id, name, or email
        response = await auth_client.get("/v1/users?search=alice")
        assert response.status_code == 200
        body = response.json()

        assert len(body["data"]) == 1, (
            f"Expected 1 match for 'alice', got {len(body['data'])}"
        )
        assert body["data"][0]["external_id"] == "alice_smith"

    # ═════════════════════════════════════════════════════════════════════
    # 10. Cross-tenant isolation
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_cross_tenant_isolation(
        self,
        app: pytest.fixture,  # noqa: ARG002
    ) -> None:
        """A user created by org A must not be accessible by org B.

        1. Create org A → api_key_A.
        2. Create org B → api_key_B.
        3. Via org A, create a user.
        4. Via org B, try to GET the same user → 404.
        """
        # -- Bootstrap two independent orgs --
        def _bootstrap(app: pytest.fixture) -> dict[str, str]:  # type: ignore[type-arg]
            """Synchronous helper to keep the fixture flow readable."""

        transport_a = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport_a, base_url="http://test") as cli:
            resp = await cli.post(
                "/admin/organizations",
                json={"name": "Org A", "plan": "free"},
            )
            assert resp.status_code == 201
            org_a = resp.json()

        transport_b = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport_b, base_url="http://test") as cli:
            resp = await cli.post(
                "/admin/organizations",
                json={"name": "Org B", "plan": "free"},
            )
            assert resp.status_code == 201
            org_b = resp.json()

        # -- Create a user under org A --
        transport_a = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport_a, base_url="http://test") as cli:
            cli.headers["Authorization"] = f"Bearer {org_a['api_key']}"
            create_resp = await cli.post(
                "/v1/users",
                json={
                    "external_id": "tenant_test_user",
                    "name": "Tenant Isolation User",
                },
            )
            assert create_resp.status_code == 201
            user_id = create_resp.json()["id"]

        # -- Try to access that user from org B → 404 --
        transport_b = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport_b, base_url="http://test") as cli:
            cli.headers["Authorization"] = f"Bearer {org_b['api_key']}"
            get_resp = await cli.get(f"/v1/users/{user_id}")

        assert get_resp.status_code == 404, (
            f"Org B should not be able to access Org A's user. "
            f"Got {get_resp.status_code}: {get_resp.text}"
        )

    # ═════════════════════════════════════════════════════════════════════
    # 11. No auth → 401
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_no_auth_returns_401(self, anon_client: AsyncClient) -> None:
        """A request without an ``Authorization`` header → 401."""
        response = await anon_client.get("/v1/users")
        assert response.status_code == 401, (
            f"Expected 401 without auth, got {response.status_code}: {response.text}"
        )
        body = response.json()
        # RFC 7807 problem-detail shape
        assert "status" in body or "detail" in body
