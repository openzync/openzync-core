"""User-dictated integration test #2: self-permission guard.

Covers:
    - Member PATCH self ``permissions`` → 422, record unchanged.
    - Admin PATCH other ``permissions`` → 200, grant capped at the
      actor's own permission set.

Real Postgres + Redis via testcontainers; real HTTP via ASGITransport.
No mocks.  The member actor is a real dashboard user whose JWT carries
``members:write`` (granted up-front by the admin) so the request
reaches the service-layer self-guard instead of stopping at the gate.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

from core.rbac import ALL_PERMISSIONS
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

_MEMBER_EMAIL = "selfperm-member@example.com"
_PASSWORD = "Member-p@ss-1"  # noqa: S105 — test fixture password, not a real credential
_MEMBER_PERMS = ["project:read", "project:write"]
_GRANTED_PERMS = ["members:read", "members:write", "project:read", "project:write"]


async def _seed_member(app: Any, org_id: UUID) -> UUID:
    """Create a verified member dashboard user with limited perms."""
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


async def _login_jwt(app: Any, email: str) -> str:
    """Password-login over real HTTP; return the access JWT."""
    transport = asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/login",
            json={"email": email, "password": _PASSWORD},
        )
    assert resp.status_code == 200, f"member login failed: {resp.text}"
    token = resp.json().get("access_token")
    assert token, "login issued no access token"
    return str(token)


class TestSelfPermissionGuard:
    """Actors cannot grant themselves permissions; grants never escalate."""

    @pytest.mark.asyncio
    async def test_member_patch_self_permissions_returns_422(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
    ) -> None:
        """Member PATCH own record ``permissions`` → 422, unchanged."""
        org_id = isolated_org_and_key["org_id"]
        admin_jwt: str = isolated_org_and_key["jwt"]
        member_id = await _seed_member(isolated_app, org_id)

        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as admin_client:
            admin_client.headers["Authorization"] = f"Bearer {admin_jwt}"
            # Grant first: without members:write the request stops at
            # the permission gate (403) instead of the self-guard (422).
            grant = await admin_client.patch(
                f"/v1/users/{member_id}",
                json={"permissions": _GRANTED_PERMS},
            )
            assert grant.status_code == 200, f"admin grant failed: {grant.text}"

        member_jwt = await _login_jwt(isolated_app, _MEMBER_EMAIL)
        async with AsyncClient(
            transport=asgi_transport(isolated_app), base_url="http://test"
        ) as member_client:
            member_client.headers["Authorization"] = f"Bearer {member_jwt}"
            resp = await member_client.patch(
                f"/v1/users/{member_id}",
                json={"permissions": ["project:read"]},
            )
            assert resp.status_code == 422, (
                f"self permission change must be 422, "
                f"got {resp.status_code}: {resp.text}"
            )
            after = await member_client.get(f"/v1/users/{member_id}")
            assert after.status_code == 200
            assert sorted(after.json()["permissions"]) == sorted(_GRANTED_PERMS), (
                "self-PATCH must not change stored permissions"
            )

    @pytest.mark.asyncio
    async def test_admin_patch_other_capped_at_actor_set(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
    ) -> None:
        """Admin PATCH other ``permissions`` → 200, within the actor set."""
        admin_jwt: str = isolated_org_and_key["jwt"]
        member_id = await _seed_member(isolated_app, isolated_org_and_key["org_id"])

        requested = [
            "configuration:read",
            "members:read",
            "members:write",
            "project:manage",
            "project:read",
            "project:write",
        ]
        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as admin_client:
            admin_client.headers["Authorization"] = f"Bearer {admin_jwt}"
            resp = await admin_client.patch(
                f"/v1/users/{member_id}",
                json={"permissions": requested},
            )
            assert resp.status_code == 200, (
                f"admin PATCH other must be 200, got {resp.status_code}: {resp.text}"
            )
            stored = resp.json()["permissions"]
            assert sorted(stored) == sorted(requested)
            # Capped at the actor set — an admin is wildcard, so the cap
            # is the full permission vocabulary.
            assert set(stored) <= set(ALL_PERMISSIONS)

    @pytest.mark.asyncio
    async def test_non_admin_patch_other_capped_at_actor_set(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
    ) -> None:
        """Non-admin PATCH other → 200, stored set is the intersection."""
        from repositories.auth_repository import AuthRepository
        from utils.password import hash_password

        org_id = isolated_org_and_key["org_id"]
        admin_jwt: str = isolated_org_and_key["jwt"]
        actor_perms = ["members:read", "members:write", "project:read"]
        async with isolated_app.state.db_session_factory() as session:
            repo = AuthRepository(session)
            actor = await repo.create_dashboard_user(
                organization_id=org_id,
                email="cap-actor@example.com",
                password_hash=hash_password(_PASSWORD),
                role="member",
                permissions=list(actor_perms),
            )
            await repo.mark_email_verified(actor.id)
            target = await repo.create_dashboard_user(
                organization_id=org_id,
                email="cap-target@example.com",
                password_hash=hash_password(_PASSWORD),
                role="member",
                permissions=["project:read"],
            )
            await repo.mark_email_verified(target.id)
            await session.commit()
            actor_id, target_id = actor.id, target.id

        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as member_client:
            member_client.headers["Authorization"] = (
                f"Bearer {await _login_jwt(isolated_app, 'cap-actor@example.com')}"
            )
            requested = ["project:read", "project:write", "members:read"]
            resp = await member_client.patch(
                f"/v1/users/{target_id}",
                json={"permissions": requested},
            )
            assert resp.status_code == 200, (
                f"non-admin PATCH other must be 200, "
                f"got {resp.status_code}: {resp.text}"
            )
            stored = resp.json()["permissions"]
            assert sorted(stored) == sorted(set(requested) & set(actor_perms)), (
                "grant must be capped at the actor's own set"
            )
            assert set(stored) <= set(actor_perms)
        _ = (admin_jwt, actor_id)
