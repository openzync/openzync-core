"""User-dictated integration test #3 (+ #9): API-key permission cap.

Covers:
    - Non-admin create key with ``members:write`` → 422 (non-project
      vocabulary is dropped, leaving an empty grant).
    - Non-admin create key with a ``project:read`` subset → 201, and a
      request above the actor set is capped down to it.
    - Fail-closed: ``get_actor_permissions`` raises 503 when app Redis
      is missing (this is also the #9 blocker-fix case — no second
      test needed).

Real Postgres + Redis via testcontainers; real HTTP via ASGITransport
for the endpoint cases.  The Redis-down case drives the real
``get_actor_permissions`` dependency with real request state.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

from tests.integration.conftest import asgi_transport


def _docker_up() -> bool:
    """Return True when a Docker daemon answers (testcontainers needs it)."""
    docker_bin = shutil.which("docker")
    if docker_bin is None:
        return False
    try:
        subprocess.run(  # noqa: S603 — fixed argv, no shell, no untrusted input
            [docker_bin, "info"],
            capture_output=True,
            timeout=30,
            check=True,
        )
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_up(),
        reason="docker unavailable — PG/Redis testcontainers cannot start; skipped, not mocked",
    ),
]

_MEMBER_EMAIL = "keycap-member@example.com"
_PASSWORD = "Keycap-p@ss-1"  # noqa: S105 — test fixture password, not a real credential
# Actor set: enough to pass the project:manage gate, missing project:write.
_MEMBER_PERMS = ["project:manage", "project:read"]


async def _seed_member(app: Any, org_id: UUID) -> UUID:
    """Create a verified member dashboard user (test infra)."""
    from repositories.auth_repository import AuthRepository
    from utils.password import hash_password

    async with app.state.db_session_factory() as session:
        repo = AuthRepository(session)
        user = await repo.create_dashboard_user(
            organization_id=org_id,
            email=_MEMBER_EMAIL,
            password_hash=hash_password(_PASSWORD),
            role="member",
            permissions=list(_MEMBER_PERMS),
        )
        await repo.mark_email_verified(user.id)
        await session.commit()
        return user.id


async def _login_jwt(app: Any) -> str:
    """Password-login the seeded member; return the access JWT."""
    transport = asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/login",
            json={"email": _MEMBER_EMAIL, "password": _PASSWORD},
        )
    assert resp.status_code == 200, f"member login failed: {resp.text}"
    return str(resp.json()["access_token"])


class TestApiKeyPermissionCap:
    """API keys never escalate above their creator's permission set."""

    @pytest.mark.asyncio
    async def test_non_admin_non_project_permission_returns_422(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
    ) -> None:
        """Non-admin key with ``members:write`` → 422, nothing minted."""
        tenant = isolated_org_and_key
        member_id = await _seed_member(isolated_app, tenant["org_id"])
        project_id = tenant["project_id"]

        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as admin_client:
            admin_client.headers["Authorization"] = f"Bearer {tenant['jwt']}"
            added = await admin_client.post(
                f"/v1/projects/{project_id}/members",
                json={"user_id": str(member_id), "role": "member"},
            )
            assert added.status_code == 201, f"add project member failed: {added.text}"

        member_jwt = await _login_jwt(isolated_app)
        async with AsyncClient(
            transport=asgi_transport(isolated_app), base_url="http://test"
        ) as member_client:
            member_client.headers["Authorization"] = f"Bearer {member_jwt}"
            resp = await member_client.post(
                f"/v1/projects/{project_id}/api-keys",
                json={"name": "escalation", "permissions": ["members:write"]},
            )
            assert resp.status_code == 422, (
                f"non-project permission grant must be 422, "
                f"got {resp.status_code}: {resp.text}"
            )
            listed = await member_client.get(f"/v1/projects/{project_id}/api-keys")

        names = [k["name"] for k in listed.json().get("data", [])]
        assert "escalation" not in names, "rejected key must not be minted"

    @pytest.mark.asyncio
    async def test_non_admin_subset_returns_201_capped(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
    ) -> None:
        """Subset grant → 201 as-is; superset request → capped to actor set."""
        tenant = isolated_org_and_key
        member_id = await _seed_member(isolated_app, tenant["org_id"])
        project_id = tenant["project_id"]

        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as admin_client:
            admin_client.headers["Authorization"] = f"Bearer {tenant['jwt']}"
            added = await admin_client.post(
                f"/v1/projects/{project_id}/members",
                json={"user_id": str(member_id), "role": "member"},
            )
            assert added.status_code == 201, f"add project member failed: {added.text}"

        member_jwt = await _login_jwt(isolated_app)
        async with AsyncClient(
            transport=asgi_transport(isolated_app), base_url="http://test"
        ) as member_client:
            member_client.headers["Authorization"] = f"Bearer {member_jwt}"
            subset = await member_client.post(
                f"/v1/projects/{project_id}/api-keys",
                json={"name": "subset", "permissions": ["project:read"]},
            )
            assert subset.status_code == 201, (
                f"subset grant must be 201, got {subset.status_code}: {subset.text}"
            )
            assert subset.json()["permissions"] == ["project:read"]

            # project:write is outside the actor set → capped off.
            capped = await member_client.post(
                f"/v1/projects/{project_id}/api-keys",
                json={
                    "name": "superset",
                    "permissions": ["project:read", "project:write"],
                },
            )
            assert capped.status_code == 201, (
                f"capped grant must still be 201, "
                f"got {capped.status_code}: {capped.text}"
            )
            assert capped.json()["permissions"] == ["project:read"], (
                "key must be capped at the creator's own set"
            )

    @pytest.mark.asyncio
    async def test_fail_closed_when_redis_down(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
    ) -> None:
        """Missing app Redis → ``get_actor_permissions`` raises 503.

        The cap now lives in the ``get_actor_permissions`` router
        dependency (``dependencies/auth.py``) — not in
        ``ApiKeyService`` (whose ``redis=None`` is a no-op cache
        bypass).  Drive the real dependency with real request state
        (JWT principal) and no Redis on the app.
        """
        from unittest.mock import AsyncMock

        from fastapi import HTTPException, Request

        from dependencies.auth import get_actor_permissions

        tenant = isolated_org_and_key
        old_redis = isolated_app.state.redis
        isolated_app.state.redis = None
        try:
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/v1/projects/x/api-keys",
                "headers": [],
                "query_string": b"",
                "server": ("test", 80),
                "scheme": "http",
                "client": ("test", 50000),
                "app": isolated_app,
            }
            request = Request(scope)
            request.state.auth_type = "jwt"
            request.state.user_id = str(tenant["user_id"])
            request.state.org_id = str(tenant["org_id"])
            with pytest.raises(HTTPException) as exc_info:
                await get_actor_permissions(
                    request,
                    db=AsyncMock(),
                )
        finally:
            isolated_app.state.redis = old_redis
        assert exc_info.value.status_code == 503
