"""User-dictated integration tests: platform:manage gate matrix (B2) + API-key gate (B5).

Frozen contract (observed live behavior):
- ``GET /v1/admin/audit-logs`` + ``GET /v1/admin/audit-logs/actions``
  require ``platform:manage`` (BREAKING: ``members:read`` no longer suffices).
- ``GET /v1/users`` requires ``platform:manage`` (listing opened to
  granted holders); ``POST /v1/users`` stays ``require_admin``
  (writes closed — a granted non-admin still 403s).
- ``GET /admin/platform-config`` requires ``configuration:read``
  (member default) — holder and plain member both 200 (UI-only tab gating).
- API keys carrying ``platform:manage`` pass ``require_permission`` on
  audit + users-list (key-permission short-circuit in ``_check_permission``).

Real Postgres + Redis via testcontainers; real HTTP via ASGITransport.
Permission grants go through superadmin PATCH + ``invalidate_permissions``
(the perms cache is 60 s TTL).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from core.rbac import MEMBER_DEFAULT_PERMISSIONS, invalidate_permissions
from dependencies.auth import _check_permission
from tests.integration.conftest import asgi_transport, superadmin_jwt

pytestmark = pytest.mark.integration


def _member_client(isolated_app: Any, jwt: str) -> AsyncClient:
    """Ad-hoc JWT-authenticated client for the isolated app."""
    transport = asgi_transport(isolated_app)
    client = AsyncClient(transport=transport, base_url="http://test")
    client.headers["Authorization"] = f"Bearer {jwt}"
    return client


def _mint_jwt(user_id: UUID) -> str:
    """Mint a member dashboard JWT for a user id (test infra)."""
    from core.config import get_settings
    from utils.crypto import create_jwt_token

    return create_jwt_token(
        data={
            "sub": str(user_id),
            "user_id": str(user_id),
            "role": "member",
            "type": "access",
        },
        secret=get_settings().SECRET_KEY,
        expires_delta=timedelta(hours=1),
    )


async def _set_permissions(
    isolated_app: Any, user_id: UUID, permissions: list[str]
) -> None:
    """Grant/revoke via superadmin PATCH, then bust the perms cache."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {await superadmin_jwt(isolated_app)}"
        resp = await c.patch(
            f"/v1/users/{user_id}", json={"permissions": permissions}
        )
    assert resp.status_code == 200, f"grant failed: {resp.text}"
    await invalidate_permissions(isolated_app.state.redis, user_id)


async def _make_member(
    isolated_app: Any, permissions: list[str] | None = None
) -> tuple[UUID, str]:
    """Create a member via superadmin, set permissions, return (id, jwt)."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {await superadmin_jwt(isolated_app)}"
        resp = await c.post(
            "/v1/users", json={"external_id": f"gate-{uuid4().hex[:8]}"}
        )
    assert resp.status_code == 201, f"member creation failed: {resp.text}"
    user_id = UUID(resp.json()["id"])
    if permissions is not None:
        await _set_permissions(isolated_app, user_id, permissions)
    return user_id, _mint_jwt(user_id)


@pytest.mark.asyncio
class TestAuditGates:
    """Audit reads are ``platform:manage``-exclusive."""

    async def test_holder_list_and_actions_200(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_admin_client: AsyncClient,
    ) -> None:
        """A granted holder reads the list and the actions dropdown."""
        user_id = isolated_org_and_key["user_id"]
        await _set_permissions(
            isolated_app,
            user_id,
            [*MEMBER_DEFAULT_PERMISSIONS, "platform:manage"],
        )
        for path in ("/v1/admin/audit-logs", "/v1/admin/audit-logs/actions"):
            resp = await isolated_admin_client.get(path)
            assert resp.status_code == 200, f"{path}: {resp.text}"

    async def test_members_read_without_grant_403(
        self, isolated_app: Any
    ) -> None:
        """``members:read`` alone no longer opens audit (pins BREAKING)."""
        _, jwt = await _make_member(
            isolated_app,
            [*MEMBER_DEFAULT_PERMISSIONS, "members:read"],
        )
        async with _member_client(isolated_app, jwt) as client:
            resp = await client.get("/v1/admin/audit-logs")
        assert resp.status_code == 403, resp.text

    async def test_anon_401(self, isolated_app: Any) -> None:
        """No auth → 401 on the audit list."""
        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/v1/admin/audit-logs")
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestUsersListGates:
    """User listing is holder-open; writes stay admin-closed."""

    async def test_holder_200(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_admin_client: AsyncClient,
    ) -> None:
        """A granted non-admin member lists users (200)."""
        await _set_permissions(
            isolated_app,
            isolated_org_and_key["user_id"],
            [*MEMBER_DEFAULT_PERMISSIONS, "platform:manage"],
        )
        resp = await isolated_admin_client.get("/v1/users")
        assert resp.status_code == 200, resp.text

    async def test_non_holder_403(
        self, isolated_app: Any, isolated_admin_client: AsyncClient
    ) -> None:
        """A plain member is denied the user list (403)."""
        resp = await isolated_admin_client.get("/v1/users")
        assert resp.status_code == 403, resp.text

    async def test_anon_401(self, isolated_app: Any) -> None:
        """No auth → 401 on the user list."""
        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/v1/users")
        assert resp.status_code == 401

    async def test_grant_then_revoke_flips_200_to_403(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_admin_client: AsyncClient,
    ) -> None:
        """Grant → 200, revoke → 403 (cache-busted both ways)."""
        user_id = isolated_org_and_key["user_id"]
        await _set_permissions(
            isolated_app, user_id, [*MEMBER_DEFAULT_PERMISSIONS, "platform:manage"]
        )
        granted = await isolated_admin_client.get("/v1/users")
        assert granted.status_code == 200, granted.text
        await _set_permissions(
            isolated_app, user_id, list(MEMBER_DEFAULT_PERMISSIONS)
        )
        revoked = await isolated_admin_client.get("/v1/users")
        assert revoked.status_code == 403, revoked.text

    async def test_post_users_granted_non_admin_still_403(
        self, isolated_app: Any
    ) -> None:
        """Writes stay closed: a holder cannot create users (403)."""
        _, jwt = await _make_member(
            isolated_app,
            [*MEMBER_DEFAULT_PERMISSIONS, "platform:manage"],
        )
        async with _member_client(isolated_app, jwt) as client:
            resp = await client.post(
                "/v1/users", json={"external_id": f"nope-{uuid4().hex[:8]}"}
            )
        assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
class TestPlatformConfigReadGates:
    """Config reads are UI-tab-gated: holder and plain member both pass."""

    async def test_holder_and_plain_member_200(
        self, isolated_app: Any, isolated_admin_client: AsyncClient
    ) -> None:
        """Holder 200; plain member 200 (``configuration:read`` default)."""
        holder_id, holder_jwt = await _make_member(
            isolated_app,
            [*MEMBER_DEFAULT_PERMISSIONS, "platform:manage"],
        )
        assert holder_id is not None
        async with _member_client(isolated_app, holder_jwt) as holder:
            holder_resp = await holder.get("/admin/platform-config")
        assert holder_resp.status_code == 200, holder_resp.text
        member_resp = await isolated_admin_client.get("/admin/platform-config")
        assert member_resp.status_code == 200, member_resp.text


@pytest.mark.unit
class TestApiKeyPlatformManage:
    """An API key carrying ``platform:manage`` passes ``require_permission``.

    Both audit-list and users-list use the same gate string, so the
    key-permission short-circuit in ``_check_permission`` covers both —
    asserted here without DB/HTTP, at the gate boundary.
    """

    @staticmethod
    def _api_key_request(permissions: list[str]) -> Any:
        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/v1/admin/audit-logs",
                "headers": [],
                "app": AsyncMock(),
            }
        )
        request.state.auth_type = "api_key"
        request.state.api_key_permissions = permissions
        return request

    @pytest.mark.asyncio
    async def test_key_with_grant_passes(self) -> None:
        """Key with the grant clears the audit/users-list gate."""
        request = self._api_key_request(["project:read", "platform:manage"])
        # No raise == pass (covers GET audit-logs and GET /v1/users).
        await _check_permission(
            request, str(uuid4()), AsyncMock(), "platform:manage"
        )

    @pytest.mark.asyncio
    async def test_key_without_grant_403(self) -> None:
        """Key without the grant is denied with 403."""
        request = self._api_key_request(["project:read"])
        with pytest.raises(HTTPException) as exc_info:
            await _check_permission(
                request, str(uuid4()), AsyncMock(), "platform:manage"
            )
        assert exc_info.value.status_code == 403
