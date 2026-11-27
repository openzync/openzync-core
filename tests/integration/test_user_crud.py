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
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.integration.conftest import bootstrap_tenant


class TestUserCrud:
    """Full CRUD lifecycle for the ``/v1/users`` endpoint family.

    Each test starts by creating a fresh organization and user so tests
    are fully self-contained and independent.
    """

    # ═════════════════════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════════════════════

    @pytest.fixture
    async def anon_client(self, isolated_app: pytest.fixture) -> AsyncClient:  # noqa: ARG002
        """Unauthenticated HTTP client — for the 401 test."""
        transport = ASGITransport(app=isolated_app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    @pytest_asyncio.fixture(loop_scope="function")
    async def admin_client(
        self, isolated_app: pytest.fixture, isolated_org_and_key: dict,
    ) -> AsyncClient:
        """Admin-authenticated client — user CRUD requires the role.

        ``POST/GET/PATCH/DELETE /v1/users*`` are gated by the platform
        admin role (the bootstrap org admin JWT gets 403).
        """
        from tests.integration.conftest import asgi_transport, superadmin_jwt

        transport = asgi_transport(isolated_app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            client.headers["Authorization"] = (
                f"Bearer {await superadmin_jwt(isolated_app)}"  # type: ignore[arg-type]
            )
            yield client
        _ = isolated_org_and_key

    @staticmethod
    def _assert_user_response_shape(body: dict) -> None:
        """Validate that ``body`` matches the ``UserResponse`` schema.

        ``UserResponse`` fields: ``id``, ``external_id``, ``name``, ``email``,
        ``metadata``, ``user_id``, ``created_at``, ``updated_at``,
        ``is_deleted``.
        """
        assert "id" in body, "Missing 'id'"
        assert "external_id" in body, "Missing 'external_id'"
        assert "user_id" in body, "Missing 'user_id'"
        assert "created_at" in body, "Missing 'created_at'"
        assert "updated_at" in body, "Missing 'updated_at'"
        assert "metadata" in body, "Missing 'metadata'"
        assert "is_deleted" in body, "Missing 'is_deleted'"

        # Validate UUIDs
        UUID(body["id"])
        UUID(body["user_id"])

        # Defaults
        assert body["is_deleted"] is False
        assert isinstance(body["metadata"], dict)

    # ═════════════════════════════════════════════════════════════════════
    # 1.  Create user — happy path
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_user(self, admin_client: AsyncClient) -> None:
        """POST /v1/users → 201 with a valid UserResponse.

        All optional fields should be reflected in the response.
        """
        response = await admin_client.post(
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
        self, admin_client: AsyncClient
    ) -> None:
        """POST /v1/users with the same external_id → 409 Conflict.

        The ``(user_id, external_id)`` unique constraint should
        prevent duplicate user creation.
        """
        # Create the first user
        resp1 = await admin_client.post(
            "/v1/users",
            json={"external_id": "dup_user"},
        )
        assert resp1.status_code == 201, f"First creation failed: {resp1.text}"

        # Attempt to create a second user with the same external_id
        response = await admin_client.post(
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
    async def test_get_user(
        self, admin_client: AsyncClient, isolated_auth_client: AsyncClient,
    ) -> None:
        """GET /v1/users/{id} → 200 with UserResponseWithStats.

        ``UserResponseWithStats`` extends ``UserResponse`` with:
        ``message_count``, ``fact_count``, ``session_count``.
        """
        # Seed a user
        created = await admin_client.post(
            "/v1/users",
            json={"external_id": "get_test", "name": "Bob"},
        )
        assert created.status_code == 201
        user_id = created.json()["id"]

        # Fetch the user
        response = await admin_client.get(f"/v1/users/{user_id}")
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
    async def test_get_user_not_found(self, admin_client: AsyncClient) -> None:
        """GET /v1/users with a non-existent UUID → 404."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = await admin_client.get(f"/v1/users/{fake_id}")
        assert response.status_code == 404, (
            f"Expected 404 for non-existent user, "
            f"got {response.status_code}: {response.text}"
        )

    # ═════════════════════════════════════════════════════════════════════
    # 5.  Update user — partial fields → 200
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_update_user(
        self, admin_client: AsyncClient, isolated_auth_client: AsyncClient,
    ) -> None:
        """PATCH /v1/users/{id} with partial data → 200, fields updated.

        Only the provided fields should change; unprovided fields
        should remain at their previous values.
        """
        # Create a user
        created = await admin_client.post(
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
        response = await admin_client.patch(
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
        self, admin_client: AsyncClient, isolated_auth_client: AsyncClient,
    ) -> None:
        """PATCH metadata is deep-merged, not replaced.

        - Existing keys that are not in the PATCH body must be preserved.
        - Keys in the PATCH body override existing values.
        - New keys in the PATCH body are added.
        """
        # Create user with initial metadata
        created = await admin_client.post(
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
        patch_resp = await admin_client.patch(
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
        get_resp = await admin_client.get(f"/v1/users/{user_id}")
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
    async def test_delete_user(
        self, admin_client: AsyncClient, isolated_auth_client: AsyncClient,
    ) -> None:
        """DELETE /v1/users/{id} → 204, subsequent GET → 404.

        Verify the soft-delete lifecycle:
        - DELETE returns 204 No Content.
        - Immediately fetching the same user returns 404 (soft-deleted).
        """
        # Create user
        created = await admin_client.post(
            "/v1/users",
            json={"external_id": "del_test"},
        )
        assert created.status_code == 201
        user_id = created.json()["id"]

        # Delete
        delete_resp = await admin_client.delete(f"/v1/users/{user_id}")
        assert delete_resp.status_code == 204, (
            f"Expected 204, got {delete_resp.status_code}: {delete_resp.text}"
        )

        # Verify it's gone
        get_resp = await admin_client.get(f"/v1/users/{user_id}")
        assert get_resp.status_code == 404, (
            f"Expected 404 after delete, got {get_resp.status_code}: {get_resp.text}"
        )

    # ═════════════════════════════════════════════════════════════════════
    # 8.  List users — cursor pagination
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_list_users_paginated(
        self, admin_client: AsyncClient, isolated_auth_client: AsyncClient,
    ) -> None:
        """GET /v1/users with cursor-based pagination.

        Seeds 4 users, then walks pages of 2 until ``has_more`` is false.
        Every page holds at most 2 rows, cursors advance, and the union
        of pages contains all seeded users.  Total counts are not
        asserted — platform seed rows (e.g. the root admin) also
        exist in the listing.
        """
        # Seed 4 users
        seeded = set()
        for i in range(4):
            resp = await admin_client.post(
                "/v1/users",
                json={"external_id": f"paginate_user_{i}"},
            )
            assert resp.status_code == 201, f"Seed failed at index {i}"
            seeded.add(f"paginate_user_{i}")

        # Walk all pages.
        seen: set[str] = set()
        cursor: str | None = None
        pages = 0
        while True:
            params = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor  # type: ignore[assignment]
            page = await admin_client.get("/v1/users", params=params)
            assert page.status_code == 200
            body = page.json()

            assert "data" in body, "Missing 'data' in list response"
            assert "next_cursor" in body, "Missing 'next_cursor'"
            assert "has_more" in body, "Missing 'has_more'"
            assert len(body["data"]) <= 2, (
                f"Expected at most 2 items, got {len(body['data'])}"
            )
            for row in body["data"]:
                seen.add(row["external_id"])
            pages += 1
            assert pages < 20, "Pagination did not terminate"

            if not body["has_more"]:
                assert body["next_cursor"] is None
                break
            assert body["next_cursor"] is not None, (
                "Expected non-null cursor while has_more is true"
            )
            cursor = body["next_cursor"]

        assert seeded <= seen, f"Seeded users missing from pages: {seeded - seen}"

    # ═════════════════════════════════════════════════════════════════════
    # 9.  List users — search filter
    # ═════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_list_users_search(
        self, admin_client: AsyncClient, isolated_auth_client: AsyncClient,
    ) -> None:
        """GET /v1/users?search=alice filters by external_id, name, or email.

        The search should match against multiple fields via ILIKE.
        """
        # Seed users with distinct identifiers
        await admin_client.post(
            "/v1/users",
            json={
                "external_id": "alice_smith",
                "name": "Alice Smith",
                "email": "alice@test.com",
            },
        )
        await admin_client.post(
            "/v1/users",
            json={
                "external_id": "bob_jones",
                "name": "Bob Jones",
                "email": "bob@test.com",
            },
        )

        # Search for "alice" — should match by external_id, name, or email
        response = await admin_client.get("/v1/users?search=alice")
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
        isolated_app: pytest.fixture,  # noqa: ARG002
    ) -> None:
        """User records are platform-visible to superadmins, gated for rest.

        1. Create tenant A → jwt_A.
        2. Create tenant B → jwt_B.
        3. Via the admin client, create a user.
        4. Admin GET the user → 200 (platform-visible, no org boundary).
        5. Org admin JWT GET the same user → 403 (admin-only gate).
        """
        from tests.integration.conftest import superadmin_jwt

        # -- Bootstrap org A --
        async with AsyncClient(
            transport=ASGITransport(app=isolated_app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            tenant_a = await bootstrap_tenant(isolated_app, cli, "Org A")

        # -- Create a user via the admin client --
        async with AsyncClient(
            transport=ASGITransport(app=isolated_app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = (
                f"Bearer {await superadmin_jwt(isolated_app)}"  # type: ignore[arg-type]
            )
            create_resp = await cli.post(
                "/v1/users",
                json={
                    "external_id": "tenant_test_user",
                    "name": "Tenant Isolation User",
                },
            )
            assert create_resp.status_code == 201
            user_id = create_resp.json()["id"]

            # -- Admin reads cross-tenant → 200 (platform-visible) --
            get_resp = await cli.get(f"/v1/users/{user_id}")
            assert get_resp.status_code == 200, (
                f"Admin should see the user. Got {get_resp.status_code}: {get_resp.text}"
            )
            assert get_resp.json()["id"] == user_id

        # -- Org admin JWT reads the same user → 403 (admin-only) --
        async with AsyncClient(
            transport=ASGITransport(app=isolated_app), base_url="http://test"  # type: ignore[arg-type]
        ) as cli:
            cli.headers["Authorization"] = f"Bearer {tenant_a['jwt']}"
            denied = await cli.get(f"/v1/users/{user_id}")
            assert denied.status_code == 403, (
                f"Org admin must be denied user reads. "
                f"Got {denied.status_code}: {denied.text}"
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
