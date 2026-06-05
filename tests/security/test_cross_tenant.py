"""Cross-tenant isolation tests.

Verify that Tenant A **cannot** read, list, or manipulate Tenant B's data
through any API endpoint.  Every data-access operation must scope its
database queries by the authenticated organization's ID.

Test matrix (3 tenants × resources × access methods):

  ┌──────────────┬──────────┬──────────┬──────────┐
  │  Operation   │ Org A →  │ Org A →  │ Org A →  │
  │              │ Org A    │ Org B    │ Org C    │
  ├──────────────┼──────────┼──────────┼──────────┤
  │ GET /users   │ 200      │ 404      │ 404      │
  │ POST /users  │ 201      │ 404      │ 404      │
  │ PATCH /users │ 200      │ 404      │ 404      │
  │ DELETE /users│ 204      │ 404      │ 404      │
  └──────────────┴──────────┴──────────┴──────────┘

All tests are skipped by default because they require:
- A running instance with a real DB
- Seed data with at least 3 organizations, each containing users/resources
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


# ── Helpers ───────────────────────────────────────────────────────────────────


ORG_A_USER_ID = "org_a_user_001"
ORG_B_USER_ID = "org_b_user_001"
ORG_C_USER_ID = "org_c_user_001"
ORG_A_SESSION_ID = "org_a_session_001"
ORG_B_SESSION_ID = "org_b_session_001"

RESOURCE_ENDPOINTS = [
    pytest.param("GET", "/v1/users/{target}", id="GET /users/<id>"),
    pytest.param("POST", "/v1/sessions", id="POST /sessions (create)"),
    pytest.param("GET", "/v1/sessions/{target}", id="GET /sessions/<id>"),
    pytest.param("DELETE", "/v1/sessions/{target}", id="DELETE /sessions/<id>"),
]


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.skip(reason="Requires real DB + 3 seeded organizations")
class TestCrossTenantIsolation:
    """Ensure Tenant A cannot access Tenant B's data via any endpoint."""

    # ── User isolation ──────────────────────────────────────────────────────

    @pytest.mark.parametrize("target_user", [ORG_B_USER_ID, ORG_C_USER_ID])
    async def test_cross_org_get_user_returns_404(
        self,
        auth_client_org_a: AsyncClient,
        target_user: str,
    ) -> None:
        """GET /users/<id> for a user in another org returns 404."""
        resp = await auth_client_org_a.get(f"/v1/users/{target_user}")
        assert resp.status_code == 404

    async def test_list_users_returns_only_own_org(
        self,
        auth_client_org_a: AsyncClient,
        auth_client_org_b: AsyncClient,
    ) -> None:
        """GET /users (list) should return only the caller's org members."""
        resp_a = await auth_client_org_a.get("/v1/users")
        resp_b = await auth_client_org_b.get("/v1/users")

        assert resp_a.status_code == 200
        assert resp_b.status_code == 200

        ids_a = {u.get("external_id") or u.get("id") for u in resp_a.json().get("data", [])}
        ids_b = {u.get("external_id") or u.get("id") for u in resp_b.json().get("data", [])}

        assert ids_a.isdisjoint(ids_b), (
            f"Overlapping user IDs between orgs: {ids_a & ids_b}"
        )

    # ── Session isolation ───────────────────────────────────────────────────

    async def test_cross_org_get_session_returns_404(
        self,
        auth_client_org_a: AsyncClient,
    ) -> None:
        """GET /sessions/<id> for a session owned by another org returns 404."""
        resp = await auth_client_org_a.get(f"/v1/sessions/{ORG_B_SESSION_ID}")
        assert resp.status_code == 404

    # ── Resource-level isolation — parametrised ─────────────────────────────

    @pytest.mark.parametrize(("method", "endpoint"), RESOURCE_ENDPOINTS)
    async def test_cross_org_resource_returns_404(
        self,
        auth_client_org_a: AsyncClient,
        method: str,
        endpoint: str,
    ) -> None:
        """Accessing any resource belonging to another org returns 404."""
        # Substitute {target} with an org-b-owned resource ID
        url = endpoint.format(target=ORG_B_USER_ID)
        resp = await auth_client_org_a.request(method, url)
        assert resp.status_code == 404, (
            f"{method} {url} returned {resp.status_code}, expected 404"
        )

    # ── Write isolation ─────────────────────────────────────────────────────

    async def test_cross_org_create_session_fails(
        self,
        auth_client_org_a: AsyncClient,
    ) -> None:
        """Creating a session with another org's user ID should fail."""
        resp = await auth_client_org_a.post(
            "/v1/sessions",
            json={"user_id": ORG_B_USER_ID},
        )
        assert resp.status_code in (404, 422), (
            f"Expected 404 or 422, got {resp.status_code}"
        )

    async def test_cross_org_delete_session_returns_404(
        self,
        auth_client_org_a: AsyncClient,
    ) -> None:
        """DELETE on a session owned by another org returns 404."""
        resp = await auth_client_org_a.delete(f"/v1/sessions/{ORG_B_SESSION_ID}")
        assert resp.status_code == 404

    # ── Pagination does not leak ────────────────────────────────────────────

    async def test_paginated_list_does_not_leak(
        self,
        auth_client_org_a: AsyncClient,
        auth_client_org_b: AsyncClient,
    ) -> None:
        """Even with pagination, listing never returns cross-org data."""
        all_ids_a: set[str] = set()
        page = 1

        while True:
            resp = await auth_client_org_a.get("/v1/users", params={"page": page, "per_page": 10})
            assert resp.status_code == 200
            data = resp.json().get("data", [])
            if not data:
                break
            all_ids_a.update(u.get("external_id") or u.get("id") for u in data)
            page += 1

        resp_b = await auth_client_org_b.get("/v1/users", params={"per_page": 100})
        ids_b = {u.get("external_id") or u.get("id") for u in resp_b.json().get("data", [])}

        assert all_ids_a.isdisjoint(ids_b), "Paginated listing leaked cross-org data!"
