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
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from core.org_config import get_org_config, update_org_config
from schemas.organization_config import OrgConfigBase, UpdateOrgConfigRequest


pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


# ═══════════════════════════════════════════════════════════════════════════════
# GET /admin/org/config
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetOrgConfig:
    """Validate reading the stored org config."""

    async def test_returns_stored_config(
        self, auth_client: AsyncClient, org_and_key: dict
    ) -> None:
        """GET should return the stored config (no env defaults)."""
        resp = await auth_client.get("/admin/org/config")
        assert resp.status_code == 200
        data = resp.json()

        # Response shape — only stored, no effective
        assert "stored" in data
        assert "effective" not in data

        # Stored should be all-None (we haven't set anything yet)
        stored = data["stored"]
        assert all(v is None for v in stored.values())

    async def test_requires_auth(self, async_client: AsyncClient) -> None:
        """Unauthenticated requests should return 401."""
        resp = await async_client.get("/admin/org/config")
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH /admin/org/config — scope-gated tests
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
            "/admin/org/config",
            json={"llm_backend": "anthropic"},
        )
        assert resp.status_code == 401

    async def test_requires_admin_write_scope(
        self, auth_client: AsyncClient
    ) -> None:
        """PATCH with read/write key (no admin:write) should return 403."""
        resp = await auth_client.patch(
            "/admin/org/config",
            json={"llm_backend": "anthropic"},
        )
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════════
# PUT /admin/org/config — scope-gated tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPutOrgConfig:
    """Validate that PUT enforces scope requirements."""

    async def test_requires_auth(self, async_client: AsyncClient) -> None:
        """PUT without auth should return 401."""
        resp = await async_client.put(
            "/admin/org/config",
            json={"llm_backend": "openai"},
        )
        assert resp.status_code == 401

    async def test_requires_admin_write_scope(
        self, auth_client: AsyncClient
    ) -> None:
        """PUT with read/write key (no admin:write) should return 403."""
        resp = await auth_client.put(
            "/admin/org/config",
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
        auth_client: AsyncClient,
        org_and_key: dict,
        app: Any,
    ) -> None:
        """A GET request should warm the Redis cache."""
        # Fetch once to warm cache
        await auth_client.get("/admin/org/config")

        # Verify cache exists
        redis = app.state.redis
        org_id = org_and_key["org_id"]
        cache_key = f"org_config:{org_id}"
        cached = await redis.get(cache_key)
        assert cached is not None, "Cache should have been warmed by the GET"

    async def test_update_invalidates_cache(
        self,
        app: Any,
        org_and_key: dict,
    ) -> None:
        """After a core-level update, the cache should be invalidated."""
        from dependencies.db import get_db

        org_id = org_and_key["org_id"]
        redis = app.state.redis
        cache_key = f"org_config:{org_id}"

        # Warm the cache by fetching via the API
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {org_and_key['api_key']}"}
            await client.get("/admin/org/config", headers=headers)

        cached_before = await redis.get(cache_key)
        assert cached_before is not None, "Cache should have been warmed"

        # Update via core function (not API — API requires admin:write scope)
        async for session in app.dependency_overrides[get_db]():
            update = UpdateOrgConfigRequest(llm_backend="anthropic")
            await update_org_config(org_id, update, session, redis=redis)

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
        """An org with no stored config should return all-None fields (no env defaults)."""
        from dependencies.db import get_db

        # Get a DB session from the app
        async for session in app.dependency_overrides[get_db]():
            config = await get_org_config(
                org_and_key["org_id"],
                session,
                redis=app.state.redis,
            )
            assert isinstance(config, OrgConfigBase)
            # Every field should be None
            for field_name in OrgConfigBase.model_fields:
                assert getattr(config, field_name) is None, (
                    f"Expected {field_name} to be None, got {getattr(config, field_name)!r}"
                )

    async def test_stored_config_reflects_db_overrides(
        self,
        app: Any,
        org_and_key: dict,
    ) -> None:
        """After a DB update, stored config should reflect the override."""
        from dependencies.db import get_db

        org_id = org_and_key["org_id"]

        async for session in app.dependency_overrides[get_db]():
            # Update via core function
            update = UpdateOrgConfigRequest(
                llm_backend="anthropic",
                llm_model="claude-3-5-sonnet",
                context_cache_ttl=120,
            )
            config = await update_org_config(
                org_id, update, session, redis=app.state.redis
            )
            assert config.llm_backend == "anthropic"
            assert config.llm_model == "claude-3-5-sonnet"
            assert config.context_cache_ttl == 120
            # Unset fields should use schema defaults (graph_backend → surrealdb)
            assert config.graph_backend == "surrealdb"
