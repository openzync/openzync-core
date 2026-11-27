"""User-dictated integration test #2: self-permission guard.

Covers (new superadmin contract — only a platform superadmin may change
``role``/``permissions``; any other actor gets 403):
    - Member PATCH self ``permissions`` → 403, record unchanged.
    - Superadmin PATCH other ``permissions`` → 200, full grant stored.
    - Org admin PATCH other ``permissions`` → 403.

Real Postgres + Redis via testcontainers; real HTTP via ASGITransport.
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


async def _seed_member(app: Any, user_id: UUID) -> UUID:
    """Create a verified member dashboard user with limited perms."""
    from repositories.auth_repository import AuthRepository
    from utils.password import hash_password

    async with app.state.db_session_factory() as session:
        repo = AuthRepository(session)
        user = await repo.create_dashboard_user(
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
    """Only superadmins change permissions; everyone else gets 403."""

    @pytest.mark.asyncio
    async def test_member_patch_self_permissions_returns_403(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
    ) -> None:
        """Member PATCH own record ``permissions`` → 403, unchanged."""
        from tests.integration.conftest import superadmin_jwt

        user_id = isolated_org_and_key["user_id"]
        member_id = await _seed_member(isolated_app, user_id)
        superadmin_headers = {
            "Authorization": f"Bearer {await superadmin_jwt(isolated_app)}"
        }

        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as super_client:
            super_client.headers.update(superadmin_headers)
            # Baseline read as superadmin (org admin JWT gets 403).
            before = await super_client.get(f"/v1/users/{member_id}")
            assert before.status_code == 200
            baseline = sorted(before.json()["permissions"])

        member_jwt = await _login_jwt(isolated_app, _MEMBER_EMAIL)
        async with AsyncClient(
            transport=asgi_transport(isolated_app), base_url="http://test"
        ) as member_client:
            member_client.headers["Authorization"] = f"Bearer {member_jwt}"
            resp = await member_client.patch(
                f"/v1/users/{member_id}",
                json={"permissions": ["project:read"]},
            )
            assert resp.status_code == 403, (
                f"self permission change must be 403, "
                f"got {resp.status_code}: {resp.text}"
            )

        async with AsyncClient(
            transport=asgi_transport(isolated_app), base_url="http://test"
        ) as super_client:
            super_client.headers.update(superadmin_headers)
            after = await super_client.get(f"/v1/users/{member_id}")
            assert after.status_code == 200
            assert sorted(after.json()["permissions"]) == baseline, (
                "self-PATCH must not change stored permissions"
            )

    @pytest.mark.asyncio
    async def test_superadmin_patch_other_full_grant(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
    ) -> None:
        """Superadmin PATCH other ``permissions`` → 200, full grant stored."""
        from tests.integration.conftest import superadmin_jwt

        member_id = await _seed_member(isolated_app, isolated_org_and_key["user_id"])

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
        ) as super_client:
            super_client.headers["Authorization"] = (
                f"Bearer {await superadmin_jwt(isolated_app)}"
            )
            resp = await super_client.patch(
                f"/v1/users/{member_id}",
                json={"permissions": requested},
            )
            assert resp.status_code == 200, (
                f"superadmin PATCH other must be 200, got {resp.status_code}: {resp.text}"
            )
            stored = resp.json()["permissions"]
            assert sorted(stored) == sorted(requested)
            # Capped at the vocabulary — a superadmin is wildcard, no cap.
            assert set(stored) <= set(ALL_PERMISSIONS)

    @pytest.mark.asyncio
    async def test_org_admin_patch_other_forbidden(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
    ) -> None:
        """Org admin (non-superadmin) PATCH other ``permissions`` → 403."""
        from repositories.auth_repository import AuthRepository
        from utils.password import hash_password

        user_id = isolated_org_and_key["user_id"]
        admin_jwt: str = isolated_org_and_key["jwt"]
        async with isolated_app.state.db_session_factory() as session:
            repo = AuthRepository(session)
            target = await repo.create_dashboard_user(
                email="cap-target@example.com",
                password_hash=hash_password(_PASSWORD),
                role="member",
                permissions=["project:read"],
            )
            await repo.mark_email_verified(target.id)
            await session.commit()
            target_id = target.id

        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as admin_client:
            admin_client.headers["Authorization"] = f"Bearer {admin_jwt}"
            resp = await admin_client.patch(
                f"/v1/users/{target_id}",
                json={"permissions": ["project:read", "project:write"]},
            )
            assert resp.status_code == 403, (
                f"non-superadmin PATCH other must be 403, "
                f"got {resp.status_code}: {resp.text}"
            )
        _ = user_id
