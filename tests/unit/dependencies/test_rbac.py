"""Unit tests for org-level RBAC — role/permission lookup, caching, gating.

Covers:
- ``core.rbac.get_org_role`` / ``invalidate_role``: cache TTL, cache-aside
  read path, fail-closed on read failures, cache-write failure does NOT
  downgrade a DB-verified admin.
- ``core.rbac.get_effective_permissions`` / ``invalidate_permissions``:
  cache-aside permission lookup, fail-closed to an empty set on Redis/DB
  read errors, cache-write failure does NOT downgrade a DB-verified set.
- ``dependencies.auth.require_permission``: JWT admin/superadmin wildcard,
  member with/without the permission, API-key path, Redis-missing 503.

All infra (Redis, DB) is mocked at the boundary — no real I/O.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException, Request

from core.rbac import (
    RBAC_PERMS_CACHE_PREFIX,
    RBAC_PERMS_CACHE_TTL,
    RBAC_ROLE_CACHE_PREFIX,
    RBAC_ROLE_CACHE_TTL,
    get_effective_permissions,
    get_org_role,
    invalidate_permissions,
    invalidate_role,
)

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
ORG_ID_STR = str(ORG_ID)
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
USER_ID_STR = str(USER_ID)
PROJECT_ID = UUID("00000000-0000-0000-0000-0000000000aa")


def _jwt_request() -> MagicMock:
    """A request with a JWT session and a Redis client on app state."""
    request = MagicMock(spec=Request)
    request.state.auth_type = "jwt"
    request.state.user_id = USER_ID_STR
    request.app.state.redis = AsyncMock()
    return request


# ═══════════════════════════════════════════════════════════════════════════════
# core.rbac.get_org_role / invalidate_role
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetOrgRole:
    """get_org_role: cache-aside role lookup with fail-closed semantics."""

    @pytest.fixture
    def db_session(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def mock_user_repo(self) -> MagicMock:
        repo = MagicMock()
        repo.get_by_uuid = AsyncMock()
        return repo

    @pytest.fixture
    def mock_user(self) -> MagicMock:
        user = MagicMock()
        user.is_active = True
        user.is_deleted = False
        user.role = "admin"
        return user

    @pytest.mark.asyncio
    async def test_cache_hit_returns_role_without_db_call(
        self, db_session: AsyncMock, mock_user_repo: MagicMock,
    ) -> None:
        """Redis returns a cached role → returned, no DB lookup."""
        redis = AsyncMock()
        redis.get.return_value = b"admin"

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            role = await get_org_role(redis, db_session, ORG_ID, USER_ID)

        assert role == "admin"
        redis.get.assert_awaited_once_with(f"{RBAC_ROLE_CACHE_PREFIX}{USER_ID}")
        mock_user_repo.get_by_uuid.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_looks_up_db_and_writes_cache(
        self, db_session: AsyncMock, mock_user_repo: MagicMock, mock_user: MagicMock,
    ) -> None:
        """Cache miss → DB lookup → role cached with the 60 s TTL."""
        redis = AsyncMock()
        redis.get.return_value = None
        mock_user_repo.get_by_uuid.return_value = mock_user

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            role = await get_org_role(redis, db_session, ORG_ID, USER_ID)

        assert role == "admin"
        mock_user_repo.get_by_uuid.assert_awaited_once_with(ORG_ID, USER_ID)
        redis.setex.assert_awaited_once_with(
            f"{RBAC_ROLE_CACHE_PREFIX}{USER_ID}", RBAC_ROLE_CACHE_TTL, "admin"
        )

    @pytest.mark.asyncio
    async def test_db_miss_returns_member(
        self, db_session: AsyncMock, mock_user_repo: MagicMock,
    ) -> None:
        """Unknown user → ``"member"`` (deny)."""
        redis = AsyncMock()
        redis.get.return_value = None
        mock_user_repo.get_by_uuid.return_value = None

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            role = await get_org_role(redis, db_session, ORG_ID, USER_ID)

        assert role == "member"
        redis.setex.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inactive_user_returns_member(
        self, db_session: AsyncMock, mock_user_repo: MagicMock, mock_user: MagicMock,
    ) -> None:
        """Inactive (or soft-deleted) user → ``"member"`` — never elevated."""
        redis = AsyncMock()
        redis.get.return_value = None
        mock_user.is_active = False
        mock_user_repo.get_by_uuid.return_value = mock_user

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            role = await get_org_role(redis, db_session, ORG_ID, USER_ID)

        assert role == "member"

    @pytest.mark.asyncio
    async def test_redis_read_failure_falls_through_to_db(
        self, db_session: AsyncMock, mock_user_repo: MagicMock, mock_user: MagicMock,
    ) -> None:
        """Redis read error → falls through to the DB (source of truth)."""
        redis = AsyncMock()
        redis.get.side_effect = ConnectionError("redis down")
        mock_user_repo.get_by_uuid.return_value = mock_user

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            role = await get_org_role(redis, db_session, ORG_ID, USER_ID)

        assert role == "admin"
        mock_user_repo.get_by_uuid.assert_awaited_once_with(ORG_ID, USER_ID)

    @pytest.mark.asyncio
    async def test_db_failure_fails_closed_to_member(
        self, db_session: AsyncMock, mock_user_repo: MagicMock,
    ) -> None:
        """DB error → ``"member"`` — a transient outage never elevates."""
        redis = AsyncMock()
        redis.get.return_value = None
        mock_user_repo.get_by_uuid.side_effect = RuntimeError("db down")

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            role = await get_org_role(redis, db_session, ORG_ID, USER_ID)

        assert role == "member"

    @pytest.mark.asyncio
    async def test_cache_write_failure_does_not_downgrade_db_verified_admin(
        self, db_session: AsyncMock, mock_user_repo: MagicMock, mock_user: MagicMock,
    ) -> None:
        """A failed cache WRITE after a successful DB read keeps the admin role.

        The role was already verified against the source of truth; the cache
        write is only an optimization.
        """
        redis = AsyncMock()
        redis.get.return_value = None
        redis.setex.side_effect = ConnectionError("redis down")
        mock_user_repo.get_by_uuid.return_value = mock_user

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            role = await get_org_role(redis, db_session, ORG_ID, USER_ID)

        assert role == "admin"


class TestInvalidateRole:
    """invalidate_role: drops the cached role key."""

    @pytest.mark.asyncio
    async def test_deletes_cache_key(self) -> None:
        """invalidate_role deletes ``rbac:role:{user_id}``."""
        redis = AsyncMock()

        await invalidate_role(redis, USER_ID)

        redis.delete.assert_awaited_once_with(f"{RBAC_ROLE_CACHE_PREFIX}{USER_ID}")

    @pytest.mark.asyncio
    async def test_redis_failure_logged_not_raised(self) -> None:
        """A Redis error during invalidation is logged, not propagated.

        The caller (role change / deletion) must not fail because the cache
        could not be cleared — the next lookup re-reads the DB anyway.
        """
        redis = AsyncMock()
        redis.delete.side_effect = ConnectionError("redis down")

        # Must not raise — the exception is caught and logged inside.
        await invalidate_role(redis, USER_ID)


# ═══════════════════════════════════════════════════════════════════════════════
# core.rbac.get_effective_permissions / invalidate_permissions
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetEffectivePermissions:
    """get_effective_permissions: cache-aside permission lookup, fail-closed."""

    @pytest.fixture
    def db_session(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def mock_user_repo(self) -> MagicMock:
        repo = MagicMock()
        repo.get_by_uuid = AsyncMock()
        return repo

    @pytest.fixture
    def mock_user(self) -> MagicMock:
        user = MagicMock()
        user.is_active = True
        user.is_deleted = False
        user.permissions = ["project:read", "project:write"]
        return user

    @pytest.mark.asyncio
    async def test_cache_hit_returns_permissions_without_db_call(
        self, db_session: AsyncMock, mock_user_repo: MagicMock,
    ) -> None:
        """Redis returns a cached JSON array → decoded, no DB lookup."""
        redis = AsyncMock()
        redis.get.return_value = b'["project:read","project:write"]'

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            perms = await get_effective_permissions(
                redis, db_session, ORG_ID, USER_ID
            )

        assert perms == frozenset({"project:read", "project:write"})
        redis.get.assert_awaited_once_with(f"{RBAC_PERMS_CACHE_PREFIX}{USER_ID}")
        mock_user_repo.get_by_uuid.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_looks_up_db_and_writes_sorted_json(
        self, db_session: AsyncMock, mock_user_repo: MagicMock, mock_user: MagicMock,
    ) -> None:
        """Cache miss → DB lookup → sorted JSON cached with the 60 s TTL."""
        redis = AsyncMock()
        redis.get.return_value = None
        mock_user_repo.get_by_uuid.return_value = mock_user

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            perms = await get_effective_permissions(
                redis, db_session, ORG_ID, USER_ID
            )

        assert perms == frozenset({"project:read", "project:write"})
        mock_user_repo.get_by_uuid.assert_awaited_once_with(ORG_ID, USER_ID)
        redis.setex.assert_awaited_once_with(
            f"{RBAC_PERMS_CACHE_PREFIX}{USER_ID}",
            RBAC_PERMS_CACHE_TTL,
            '["project:read", "project:write"]',
        )

    @pytest.mark.asyncio
    async def test_db_miss_returns_empty_set(
        self, db_session: AsyncMock, mock_user_repo: MagicMock,
    ) -> None:
        """Unknown user → empty frozenset (deny)."""
        redis = AsyncMock()
        redis.get.return_value = None
        mock_user_repo.get_by_uuid.return_value = None

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            perms = await get_effective_permissions(
                redis, db_session, ORG_ID, USER_ID
            )

        assert perms == frozenset()
        redis.setex.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inactive_user_returns_empty_set(
        self, db_session: AsyncMock, mock_user_repo: MagicMock, mock_user: MagicMock,
    ) -> None:
        """Inactive (or soft-deleted) user → empty set — never granted."""
        redis = AsyncMock()
        redis.get.return_value = None
        mock_user.is_active = False
        mock_user_repo.get_by_uuid.return_value = mock_user

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            perms = await get_effective_permissions(
                redis, db_session, ORG_ID, USER_ID
            )

        assert perms == frozenset()

    @pytest.mark.asyncio
    async def test_redis_read_failure_falls_through_to_db(
        self, db_session: AsyncMock, mock_user_repo: MagicMock, mock_user: MagicMock,
    ) -> None:
        """Redis read error → falls through to the DB (source of truth)."""
        redis = AsyncMock()
        redis.get.side_effect = ConnectionError("redis down")
        mock_user_repo.get_by_uuid.return_value = mock_user

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            perms = await get_effective_permissions(
                redis, db_session, ORG_ID, USER_ID
            )

        assert perms == frozenset({"project:read", "project:write"})
        mock_user_repo.get_by_uuid.assert_awaited_once_with(ORG_ID, USER_ID)

    @pytest.mark.asyncio
    async def test_db_failure_fails_closed_to_empty(
        self, db_session: AsyncMock, mock_user_repo: MagicMock,
    ) -> None:
        """DB error → empty frozenset — an outage never grants permissions."""
        redis = AsyncMock()
        redis.get.return_value = None
        mock_user_repo.get_by_uuid.side_effect = RuntimeError("db down")

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            perms = await get_effective_permissions(
                redis, db_session, ORG_ID, USER_ID
            )

        assert perms == frozenset()

    @pytest.mark.asyncio
    async def test_cache_write_failure_does_not_downgrade_db_verified_set(
        self, db_session: AsyncMock, mock_user_repo: MagicMock, mock_user: MagicMock,
    ) -> None:
        """A failed cache WRITE after a successful DB read keeps the set.

        The permissions were already verified against the source of truth;
        the cache write is only an optimization.
        """
        redis = AsyncMock()
        redis.get.return_value = None
        redis.setex.side_effect = ConnectionError("redis down")
        mock_user_repo.get_by_uuid.return_value = mock_user

        with patch("core.rbac.UserRepository", return_value=mock_user_repo):
            perms = await get_effective_permissions(
                redis, db_session, ORG_ID, USER_ID
            )

        assert perms == frozenset({"project:read", "project:write"})


class TestInvalidatePermissions:
    """invalidate_permissions: drops the cached permission key."""

    @pytest.mark.asyncio
    async def test_deletes_cache_key(self) -> None:
        """invalidate_permissions deletes ``rbac:perms:{user_id}``."""
        redis = AsyncMock()

        await invalidate_permissions(redis, USER_ID)

        redis.delete.assert_awaited_once_with(f"{RBAC_PERMS_CACHE_PREFIX}{USER_ID}")

    @pytest.mark.asyncio
    async def test_redis_failure_logged_not_raised(self) -> None:
        """A Redis error during invalidation is logged, not propagated."""
        redis = AsyncMock()
        redis.delete.side_effect = ConnectionError("redis down")

        # Must not raise — the exception is caught and logged inside.
        await invalidate_permissions(redis, USER_ID)


# ═══════════════════════════════════════════════════════════════════════════════
# dependencies.auth.require_permission
# ═══════════════════════════════════════════════════════════════════════════════


class TestRequirePermission:
    """require_permission: JWT role wildcard + explicit perms, API-key list."""

    @pytest.fixture
    def db_session(self) -> AsyncMock:
        return AsyncMock(spec=__import__(
            "sqlalchemy.ext.asyncio", fromlist=["AsyncSession"]
        ).AsyncSession)

    @pytest.mark.asyncio
    async def test_jwt_admin_role_passes_any_permission(
        self, db_session: AsyncMock,
    ) -> None:
        """JWT user with the org admin role → wildcard, returns the org_id."""
        from dependencies.auth import require_permission

        checker = require_permission("configuration:write")
        request = _jwt_request()
        with (
            patch(
                "dependencies.auth.get_org_role", new=AsyncMock(return_value="admin")
            ) as mock_get_role,
            patch(
                "dependencies.auth.get_effective_permissions",
                new=AsyncMock(side_effect=AssertionError("must not be called")),
            ),
        ):
            result = await checker(request, ORG_ID_STR, db_session)

        assert result == ORG_ID_STR
        mock_get_role.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_jwt_superadmin_role_passes_any_permission(
        self, db_session: AsyncMock,
    ) -> None:
        """JWT user with the superadmin role → wildcard, passes."""
        from dependencies.auth import require_permission

        checker = require_permission("members:write")
        request = _jwt_request()
        with patch(
            "dependencies.auth.get_org_role", new=AsyncMock(return_value="superadmin")
        ):
            result = await checker(request, ORG_ID_STR, db_session)
        assert result == ORG_ID_STR

    @pytest.mark.asyncio
    async def test_jwt_member_with_permission_passes(
        self, db_session: AsyncMock,
    ) -> None:
        """JWT member whose explicit permissions contain the required one → passes."""
        from dependencies.auth import require_permission

        checker = require_permission("project:write")
        request = _jwt_request()
        with (
            patch(
                "dependencies.auth.get_org_role", new=AsyncMock(return_value="member")
            ),
            patch(
                "dependencies.auth.get_effective_permissions",
                new=AsyncMock(return_value=frozenset({"project:read", "project:write"})),
            ),
        ):
            result = await checker(request, ORG_ID_STR, db_session)
        assert result == ORG_ID_STR

    @pytest.mark.asyncio
    async def test_jwt_member_without_permission_raises_403(
        self, db_session: AsyncMock,
    ) -> None:
        """JWT member lacking the permission → 403."""
        from dependencies.auth import require_permission

        checker = require_permission("configuration:read")
        request = _jwt_request()
        with (
            patch(
                "dependencies.auth.get_org_role", new=AsyncMock(return_value="member")
            ),
            patch(
                "dependencies.auth.get_effective_permissions",
                new=AsyncMock(return_value=frozenset({"project:read"})),
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await checker(request, ORG_ID_STR, db_session)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_api_key_with_permission_passes(self, db_session: AsyncMock) -> None:
        """API key whose permission list contains the required one → passes."""
        from dependencies.auth import require_permission

        checker = require_permission("project:read")
        request = _jwt_request()
        request.state.auth_type = "api_key"
        request.state.api_key_permissions = ["project:read", "project:write"]

        result = await checker(request, ORG_ID_STR, db_session)
        assert result == ORG_ID_STR

    @pytest.mark.asyncio
    async def test_api_key_without_permission_raises_403(
        self, db_session: AsyncMock,
    ) -> None:
        """API key missing the permission → 403 naming key + available set."""
        from dependencies.auth import require_permission

        checker = require_permission("project:write")
        request = _jwt_request()
        request.state.auth_type = "api_key"
        request.state.api_key_permissions = ["project:read"]

        with pytest.raises(HTTPException) as exc:
            await checker(request, ORG_ID_STR, db_session)
        assert exc.value.status_code == 403
        assert "project:write" in exc.value.detail["detail"]
        assert "project:read" in exc.value.detail["detail"]

    @pytest.mark.asyncio
    async def test_jwt_missing_user_id_raises_401(self, db_session: AsyncMock) -> None:
        """JWT session without a user id → 401."""
        from dependencies.auth import require_permission

        checker = require_permission("project:read")
        request = _jwt_request()
        request.state.user_id = None

        with pytest.raises(HTTPException) as exc:
            await checker(request, ORG_ID_STR, db_session)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_redis_on_app_state_raises_503(
        self, db_session: AsyncMock,
    ) -> None:
        """Redis not configured on app.state → 503 (explicit, never silent)."""
        from dependencies.auth import require_permission

        checker = require_permission("project:read")
        request = _jwt_request()
        request.app.state.redis = None

        with pytest.raises(HTTPException) as exc:
            await checker(request, ORG_ID_STR, db_session)
        assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_infra_failure_fails_closed_to_403(self, db_session: AsyncMock) -> None:
        """Role lookup fails closed to member + empty perms → 403 deny."""
        from dependencies.auth import require_permission

        checker = require_permission("project:read")
        request = _jwt_request()
        with (
            patch(
                "dependencies.auth.get_org_role", new=AsyncMock(return_value="member")
            ),
            patch(
                "dependencies.auth.get_effective_permissions",
                new=AsyncMock(return_value=frozenset()),
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await checker(request, ORG_ID_STR, db_session)
        assert exc.value.status_code == 403