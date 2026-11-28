"""Integration tests for the admin org config REST API.

Tests depend on testcontainers (PostgreSQL + Redis) bootstrapped in
``conftest.py`` and run against a real FastAPI test client.

.. note::

    The bootstrap ``POST /admin/organizations`` generates an API key with
    scopes ``['read', 'write', 'admin']`` — **not** ``['admin:write']``.
    Since ``require_scope("admin:write")`` uses exact-string matching, the
    PATCH/PUT endpoints (guarded by ``admin:write``) will **not** work with
    the bootstrap key.  Those tests are exercised indirectly via the core
    resolution functions in ``TestResolutionEndToEnd``.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from core.platform_config import get_platform_config, update_platform_config
from schemas.platform_config import PlatformConfigBase, UpdatePlatformConfigRequest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def jwt_client(
    app: Any, org_and_key: dict,
) -> AsyncClient:
    """JWT-authenticated client — org config reads require ``configuration:read``.

    ``GET /admin/platform-config`` is gated by
    ``require_permission("configuration:read")`` — the project-scoped API
    key lacks it, so config reads use the bootstrap member JWT (the
    permission is in the member default grant set).
    """
    from tests.integration.conftest import asgi_transport

    transport = asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = (
            f"Bearer {org_and_key['jwt']}"
        )
        yield client


# ═══════════════════════════════════════════════════════════════════════════════
# GET /admin/platform-config
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetOrgConfig:
    """Validate reading the stored org config."""

    async def test_returns_stored_config(
        self, jwt_client: AsyncClient, org_and_key: dict
    ) -> None:
        """GET should return the stored config (no env defaults)."""
        resp = await jwt_client.get("/admin/platform-config")
        assert resp.status_code == 200
        data = resp.json()

        # Response shape — only stored, no effective
        assert "stored" in data
        assert "effective" not in data

        # Fresh org: get_platform_config defaults graph_backend to
        # 'falkordb' for empty configs (core/platform_config.py); every
        # other field stays None until explicitly set.
        stored = data["stored"]
        assert stored["graph_backend"] == "falkordb"
        assert all(
            v is None for k, v in stored.items() if k != "graph_backend"
        )

    async def test_requires_auth(self, async_client: AsyncClient) -> None:
        """Unauthenticated requests should return 401."""
        resp = await async_client.get("/admin/platform-config")
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH /admin/platform-config — scope-gated tests
# ═══════════════════════════════════════════════════════════════════════════════
# The bootstrap API key has scopes ['read', 'write', 'admin'] (not
# 'admin:write'), so PATCH/PUT return 403.  The happy path for config
# updates is tested via core functions in TestResolutionEndToEnd.


class TestPatchOrgConfig:
    """Validate that PATCH enforces scope requirements.

    .. note::

        The bootstrap API key has scopes ``['read', 'write', 'admin']``,
        not ``'admin:write'``, so the scope check raises **403** before
        any input validation (422) can occur.  Invalid-value tests are
        covered by unit tests instead.
    """

    async def test_requires_auth(self, async_client: AsyncClient) -> None:
        """PATCH without auth should return 401."""
        resp = await async_client.patch(
            "/admin/platform-config",
            json={"llm_backend": "anthropic"},
        )
        assert resp.status_code == 401

    async def test_requires_admin_write_scope(
        self, auth_client: AsyncClient
    ) -> None:
        """PATCH with read/write key (no admin:write) should return 403."""
        resp = await auth_client.patch(
            "/admin/platform-config",
            json={"llm_backend": "anthropic"},
        )
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════════
# PUT /admin/platform-config — scope-gated tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPutOrgConfig:
    """Validate that PUT enforces scope requirements."""

    async def test_requires_auth(self, async_client: AsyncClient) -> None:
        """PUT without auth should return 401."""
        resp = await async_client.put(
            "/admin/platform-config",
            json={"llm_backend": "openai"},
        )
        assert resp.status_code == 401

    async def test_requires_admin_write_scope(
        self, auth_client: AsyncClient
    ) -> None:
        """PUT with read/write key (no admin:write) should return 403."""
        resp = await auth_client.put(
            "/admin/platform-config",
            json={"llm_backend": "openai"},
        )
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════════
# Redis cache invalidation
# ═══════════════════════════════════════════════════════════════════════════════


class TestCacheInvalidation:
    """Updates should invalidate Redis cache so subsequent reads get fresh data."""

    async def test_get_warms_cache(
        self,
        jwt_client: AsyncClient,
        org_and_key: dict,
        app: Any,
    ) -> None:
        """A GET request should warm the Redis cache."""
        # Fetch once to warm cache
        await jwt_client.get("/admin/platform-config")

        # Verify cache exists
        redis = app.state.redis
        user_id = org_and_key["user_id"]
        cache_key = f"platform_config:{user_id}"
        cached = await redis.get(cache_key)
        assert cached is not None, "Cache should have been warmed by the GET"

    async def test_update_invalidates_cache(
        self,
        app: Any,
        org_and_key: dict,
    ) -> None:
        """After a core-level update, the cache should be invalidated."""
        user_id = org_and_key["user_id"]
        redis = app.state.redis
        cache_key = f"platform_config:{user_id}"

        # Warm the cache by fetching via the API
        from tests.integration.conftest import asgi_transport

        transport = asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {org_and_key['jwt']}"}
            await client.get("/admin/platform-config", headers=headers)

        cached_before = await redis.get(cache_key)
        assert cached_before is not None, "Cache should have been warmed"

        # Update via core function against the real DB row.
        update = UpdatePlatformConfigRequest(llm_backend="anthropic", llm_api_key="sk-test")
        async with app.state.db_session_factory() as db:
            await update_platform_config(user_id, update, redis=redis, db=db)
            await db.commit()

        # Cache should be invalidated
        cached_after = await redis.get(cache_key)
        assert cached_after is None, "Cache should have been invalidated after update"


# ═══════════════════════════════════════════════════════════════════════════════
# Resolution correctness (end-to-end)
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolutionEndToEnd:
    """End-to-end tests exercising the full resolution pipeline via core functions."""

    async def test_no_stored_config_returns_all_none(
        self,
        app: Any,
        org_and_key: dict,
    ) -> None:
        """An org with no stored config defaults graph_backend to falkordb."""
        async with app.state.db_session_factory() as db:
            config = await get_platform_config(
                org_and_key["user_id"],
                redis=app.state.redis,
                db=db,
            )
        assert isinstance(config, PlatformConfigBase)
        # Empty config defaults graph_backend to 'falkordb'
        # (core/platform_config.py) — every other field should be None.
        for field_name in PlatformConfigBase.model_fields:
            expected = "falkordb" if field_name == "graph_backend" else None
            assert getattr(config, field_name) == expected, (
                f"Expected {field_name} to be {expected!r}, "
                f"got {getattr(config, field_name)!r}"
            )

    async def test_stored_config_reflects_db_overrides(
        self,
        app: Any,
        org_and_key: dict,
    ) -> None:
        """After a DB update, stored config should reflect the override."""
        user_id = org_and_key["user_id"]

        update = UpdatePlatformConfigRequest(
            llm_backend="anthropic",
            llm_model="claude-3-5-sonnet",
            llm_api_key="sk-test",
            context_cache_ttl=120,
        )
        async with app.state.db_session_factory() as db:
            config = await update_platform_config(
                user_id, update, redis=app.state.redis, db=db
            )
            await db.commit()
        assert config.llm_backend == "anthropic"
        assert config.llm_model == "claude-3-5-sonnet"
        assert config.context_cache_ttl == 120
