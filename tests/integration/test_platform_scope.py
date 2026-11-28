"""User-dictated integration tests: ``resolve_platform_scope`` matrix (B3).

Frozen contract (observed live behavior) across the platform reads
(``GET /v1/admin/stats``, ``GET /v1/admin/llm-usage``, ``GET
/metrics/summary``, ``GET /metrics/batch``):

- both ``?user_id=`` + ``?project_id=`` → 422;
- non-holder ``?user_id=`` → 403; holder ``?user_id=`` → 200 narrowed;
- fake ``?project_id=`` → 404 for root/holder/plain member;
- foreign-real ``?project_id=`` → 403 for all (owner-only, even holders);
- holder default aggregates every user's rows; member default is own-only.

Real Postgres + Redis via testcontainers; real HTTP via ASGITransport.
Two members are seeded (bootstrap user1 + created user2), each owning a
project with a known episode count.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from core.rbac import MEMBER_DEFAULT_PERMISSIONS, invalidate_permissions
from tests.integration.conftest import asgi_transport, superadmin_jwt

pytestmark = pytest.mark.integration

# (method, path, base params) — every platform read in the scope contract.
# ``/metrics/batch`` requires an explicit window; the rest default it.
ENDPOINTS: list[tuple[str, dict[str, str]]] = [
    ("/v1/admin/stats", {}),
    ("/v1/admin/llm-usage", {}),
    ("/metrics/summary", {}),
    ("/metrics/batch", {"days": "7"}),
]


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


async def _grant_holder(isolated_app: Any, user_id: UUID) -> None:
    """Grant ``platform:manage`` via superadmin PATCH + cache bust."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {await superadmin_jwt(isolated_app)}"
        resp = await c.patch(
            f"/v1/users/{user_id}",
            json={"permissions": [*MEMBER_DEFAULT_PERMISSIONS, "platform:manage"]},
        )
    assert resp.status_code == 200, f"grant failed: {resp.text}"
    await invalidate_permissions(isolated_app.state.redis, user_id)


async def _seed_user_with_episodes(
    isolated_app: Any, n_episodes: int
) -> tuple[UUID, str, UUID]:
    """Create a member + project + session, seed ``n_episodes``; return ids+jwt.

    The user is created via superadmin POST (always role ``member``),
    the project/session via the user's own JWT, episodes via the
    repository (same seam as ``test_sorting_parity``).
    """
    from repositories.episode_repository import EpisodeRepository

    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {await superadmin_jwt(isolated_app)}"
        resp = await c.post(
            "/v1/users", json={"external_id": f"scope-{uuid4().hex[:8]}"}
        )
    assert resp.status_code == 201, f"member creation failed: {resp.text}"
    user_id = UUID(resp.json()["id"])
    jwt = _mint_jwt(user_id)

    async with _member_client(isolated_app, jwt) as client:
        proj = await client.post(
            "/v1/projects", json={"name": f"Scope {uuid4().hex[:8]}"}
        )
        assert proj.status_code == 201, proj.text
        project_id = UUID(proj.json()["id"])
        sess = await client.post(
            f"/v1/projects/{project_id}/sessions",
            json={"external_id": f"scope-sess-{uuid4().hex[:8]}"},
        )
        assert sess.status_code == 201, sess.text
        session_id = UUID(sess.json()["id"])

    async with isolated_app.state.db_session_factory() as db:
        repo = EpisodeRepository(db)
        await repo.batch_create(
            project_id=project_id,
            session_id=session_id,
            user_id=user_id,
            messages=[
                {"role": "user", "content": f"scope ep {i}", "metadata": {}}
                for i in range(n_episodes)
            ],
        )
        await db.commit()
    return user_id, jwt, project_id


@pytest.mark.asyncio
class TestScopeStatusMatrix:
    """Status-code sweeps for the scope filters, parametrized per endpoint."""

    @pytest.mark.filterwarnings(
        "ignore::starlette.exceptions.StarletteDeprecationWarning"
    )
    @pytest.mark.parametrize("path,base", ENDPOINTS)
    async def test_both_filters_422(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        path: str,
        base: dict[str, str],
    ) -> None:
        """Holder passing ``?user_id=`` + ``?project_id=`` → 422."""
        await _grant_holder(isolated_app, isolated_org_and_key["user_id"])
        other_id, _, _ = await _seed_user_with_episodes(isolated_app, 1)
        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            client.headers["Authorization"] = (
                f"Bearer {isolated_org_and_key['jwt']}"
            )
            resp = await client.get(
                path,
                params={
                    **base,
                    "user_id": str(other_id),
                    "project_id": str(isolated_org_and_key["project_id"]),
                },
            )
        assert resp.status_code == 422, f"{path}: {resp.text}"

    @pytest.mark.parametrize("path,base", ENDPOINTS)
    async def test_non_holder_user_id_403(
        self, isolated_app: Any, path: str, base: dict[str, str]
    ) -> None:
        """A plain member filtering by another user → 403."""
        user_id, jwt, _ = await _seed_user_with_episodes(isolated_app, 1)
        other_id, _, _ = await _seed_user_with_episodes(isolated_app, 1)
        assert user_id != other_id
        async with _member_client(isolated_app, jwt) as client:
            resp = await client.get(
                path, params={**base, "user_id": str(other_id)}
            )
        assert resp.status_code == 403, f"{path}: {resp.text}"

    @pytest.mark.parametrize("path,base", ENDPOINTS)
    async def test_fake_project_id_404_for_all_callers(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_admin_client: AsyncClient,
        isolated_superadmin_client: AsyncClient,
        path: str,
        base: dict[str, str],
    ) -> None:
        """Unknown ``?project_id=`` → 404 for root, holder, and member alike."""
        await _grant_holder(isolated_app, isolated_org_and_key["user_id"])
        _, plain_jwt, _ = await _seed_user_with_episodes(isolated_app, 1)
        fake = str(uuid4())
        async with _member_client(isolated_app, plain_jwt) as plain:
            callers = [
                isolated_superadmin_client,  # root
                isolated_admin_client,  # holder (granted above)
                plain,  # plain member
            ]
            for caller in callers:
                resp = await caller.get(
                    path, params={**base, "project_id": fake}
                )
                assert resp.status_code == 404, f"{path}: {resp.text}"

    async def test_foreign_project_id_403_for_all_callers(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_admin_client: AsyncClient,
        isolated_superadmin_client: AsyncClient,
    ) -> None:
        """A real project owned by someone else → 403 for everyone.

        Owner-validation runs for ALL callers — holders included (they
        read a foreign project no more than anyone else; their own
        rows stay visible through the unfiltered scope).
        """
        await _grant_holder(isolated_app, isolated_org_and_key["user_id"])
        _, plain_jwt, foreign_project = await _seed_user_with_episodes(
            isolated_app, 1
        )
        assert foreign_project != isolated_org_and_key["project_id"]
        async with _member_client(isolated_app, plain_jwt) as plain:
            # holder reads the plain member's project → 403
            holder_resp = await isolated_admin_client.get(
                "/v1/admin/stats", params={"project_id": str(foreign_project)}
            )
            assert holder_resp.status_code == 403, holder_resp.text
            # root reads it → 403
            root_resp = await isolated_superadmin_client.get(
                "/v1/admin/stats", params={"project_id": str(foreign_project)}
            )
            assert root_resp.status_code == 403, root_resp.text
            # plain member reads the holder's project → 403
            plain_resp = await plain.get(
                "/v1/admin/stats",
                params={"project_id": str(isolated_org_and_key["project_id"])},
            )
            assert plain_resp.status_code == 403, plain_resp.text


@pytest.mark.asyncio
class TestScopeRowVisibility:
    """Row-level narrowing: holder sees the union, members see their own."""

    async def test_holder_user_id_200_narrowed(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_admin_client: AsyncClient,
    ) -> None:
        """Holder ``?user_id=`` scopes the read to that user only."""
        await _grant_holder(isolated_app, isolated_org_and_key["user_id"])
        # user1 (bootstrap) seeds its own episodes via its own project.
        from repositories.episode_repository import EpisodeRepository

        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            client.headers["Authorization"] = (
                f"Bearer {isolated_org_and_key['jwt']}"
            )
            sess = await client.post(
                f"/v1/projects/{isolated_org_and_key['project_id']}/sessions",
                json={"external_id": f"scope-own-{uuid4().hex[:8]}"},
            )
            assert sess.status_code == 201, sess.text
            own_session = UUID(sess.json()["id"])
        async with isolated_app.state.db_session_factory() as db:
            await EpisodeRepository(db).batch_create(
                project_id=isolated_org_and_key["project_id"],
                session_id=own_session,
                user_id=isolated_org_and_key["user_id"],
                messages=[
                    {"role": "user", "content": "own ep 1", "metadata": {}},
                    {"role": "user", "content": "own ep 2", "metadata": {}},
                ],
            )
            await db.commit()
        other_id, _, _ = await _seed_user_with_episodes(isolated_app, 3)

        resp = await isolated_admin_client.get(
            "/v1/admin/stats", params={"user_id": str(other_id)}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["total_episodes"] == 3

    async def test_holder_default_aggregates_both_users(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_admin_client: AsyncClient,
    ) -> None:
        """Holder with no filters sees the union of both users' rows."""
        await _grant_holder(isolated_app, isolated_org_and_key["user_id"])
        from repositories.episode_repository import EpisodeRepository

        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            client.headers["Authorization"] = (
                f"Bearer {isolated_org_and_key['jwt']}"
            )
            sess = await client.post(
                f"/v1/projects/{isolated_org_and_key['project_id']}/sessions",
                json={"external_id": f"scope-union-{uuid4().hex[:8]}"},
            )
            assert sess.status_code == 201, sess.text
            own_session = UUID(sess.json()["id"])
        async with isolated_app.state.db_session_factory() as db:
            await EpisodeRepository(db).batch_create(
                project_id=isolated_org_and_key["project_id"],
                session_id=own_session,
                user_id=isolated_org_and_key["user_id"],
                messages=[
                    {"role": "user", "content": "union own 1", "metadata": {}},
                    {"role": "user", "content": "union own 2", "metadata": {}},
                ],
            )
            await db.commit()
        await _seed_user_with_episodes(isolated_app, 3)
        resp = await isolated_admin_client.get("/v1/admin/stats")
        assert resp.status_code == 200, resp.text
        assert resp.json()["total_episodes"] == 5

    async def test_member_default_own_only(self, isolated_app: Any) -> None:
        """A plain member with no filters sees only their own rows."""
        _, jwt, _ = await _seed_user_with_episodes(isolated_app, 3)
        await _seed_user_with_episodes(isolated_app, 2)
        async with _member_client(isolated_app, jwt) as client:
            resp = await client.get("/v1/admin/stats")
        assert resp.status_code == 200, resp.text
        assert resp.json()["total_episodes"] == 3
