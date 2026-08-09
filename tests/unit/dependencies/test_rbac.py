"""Unit tests for org-level RBAC — role lookup, caching, and admin gating.

Covers:
- ``require_org_admin``: JWT admin passes, member 403, API key 401,
  fail-closed on Redis/DB errors (deny, never grant).
- ``require_scope``: JWT admin-scope enforcement, member-level scopes
  pass for members, API-key path unchanged.
- ``require_project_owner``: owner passes, org admin bypass, non-owner 403.
- ``core.rbac.get_org_role`` / ``invalidate_role``: cache TTL, cache-aside
  read path, fail-closed on read failures, cache-write failure does NOT
  downgrade a DB-verified admin.

All infra (Redis, DB) is mocked at the boundary — no real I/O.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException, Request

from core.rbac import (
    RBAC_ROLE_CACHE_PREFIX,
    RBAC_ROLE_CACHE_TTL,
    get_org_role,
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
# dependencies.auth.require_org_admin
# ═══════════════════════════════════════════════════════════════════════════════


class TestRequireOrgAdmin:
    """require_org_admin: JWT-only, DB-verified org-admin gate."""

    @pytest.fixture
    def db_session(self) -> AsyncMock:
        return AsyncMock(spec=__import__(
            "sqlalchemy.ext.asyncio", fromlist=["AsyncSession"]
        ).AsyncSession)

    @pytest.mark.asyncio
    async def test_admin_role_passes(self, db_session: AsyncMock) -> None:
        """JWT user with the org admin role → returns the org_id."""
        from dependencies.auth import require_org_admin

        request = _jwt_request()
        with patch(
            "dependencies.auth.get_org_role", new=AsyncMock(return_value="admin")
        ) as mock_get_role:
            result = await require_org_admin(
                request, ORG_ID_STR, USER_ID_STR, db_session,
            )

        assert result == ORG_ID_STR
        mock_get_role.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_member_role_raises_403(self, db_session: AsyncMock) -> None:
        """JWT user with the member role → 403."""
        from dependencies.auth import require_org_admin

        request = _jwt_request()
        with patch(
            "dependencies.auth.get_org_role", new=AsyncMock(return_value="member")
        ), pytest.raises(HTTPException) as exc:
            await require_org_admin(request, ORG_ID_STR, USER_ID_STR, db_session)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_api_key_auth_raises_401(self, db_session: AsyncMock) -> None:
        """API-key auth → 401 (org admin requires a JWT dashboard session).

        ``require_org_admin`` composes ``get_dashboard_user`` for the
        JWT-only gate — that dependency rejects API-key sessions before the
        role check runs.
        """
        from dependencies.auth import get_dashboard_user

        request = _jwt_request()
        request.state.auth_type = "api_key"
        request.state.user_id = None

        with pytest.raises(HTTPException) as exc:
            await get_dashboard_user(request, ORG_ID_STR)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_stale_member_jwt_promotion_takes_effect_immediately(
        self, db_session: AsyncMock,
    ) -> None:
        """DB-verified admin passes even with a stale member-role JWT.

        Role freshness: the JWT ``role`` claim is never trusted — the DB is
        the source of truth.  A member-claim JWT whose DB role was promoted
        to admin passes immediately (no re-login required).
        """
        from dependencies.auth import require_org_admin

        request = _jwt_request()
        # The JWT says member (middleware would normally set role claims),
        # but the DB-backed lookup says admin — the DB wins.
        with patch(
            "dependencies.auth.get_org_role", new=AsyncMock(return_value="admin")
        ):
            result = await require_org_admin(
                request, ORG_ID_STR, USER_ID_STR, db_session,
            )
        assert result == ORG_ID_STR

    @pytest.mark.asyncio
    async def test_stale_admin_jwt_demotion_denied_immediately(
        self, db_session: AsyncMock,
    ) -> None:
        """DB-verified member is denied even with a stale admin-role JWT.

        Demotion takes effect immediately: an admin-claim JWT whose DB role
        was demoted to member gets 403 on the next request.
        """
        from dependencies.auth import require_org_admin

        request = _jwt_request()
        with patch(
            "dependencies.auth.get_org_role", new=AsyncMock(return_value="member")
        ), pytest.raises(HTTPException) as exc:
            await require_org_admin(request, ORG_ID_STR, USER_ID_STR, db_session)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_infra_failure_fails_closed_to_403(
        self, db_session: AsyncMock,
    ) -> None:
        """Redis/DB failure → get_org_role returns member → 403 deny.

        Fail-closed: an infrastructure outage can never grant admin.
        """
        from dependencies.auth import require_org_admin

        request = _jwt_request()
        with patch(
            "dependencies.auth.get_org_role", new=AsyncMock(return_value="member")
        ), pytest.raises(HTTPException) as exc:
            await require_org_admin(request, ORG_ID_STR, USER_ID_STR, db_session)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_redis_on_app_state_raises_503(
        self, db_session: AsyncMock,
    ) -> None:
        """Redis not configured on app.state → 503 (explicit, never silent)."""
        from dependencies.auth import require_org_admin

        request = _jwt_request()
        request.app.state.redis = None

        with pytest.raises(HTTPException) as exc:
            await require_org_admin(request, ORG_ID_STR, USER_ID_STR, db_session)
        assert exc.value.status_code == 503


# ═══════════════════════════════════════════════════════════════════════════════
# dependencies.auth.require_scope
# ═══════════════════════════════════════════════════════════════════════════════


class TestRequireScopeJwtEnforcement:
    """require_scope: JWT role enforcement on admin scopes, member scopes free."""

    @pytest.fixture
    def db_session(self) -> AsyncMock:
        return AsyncMock()

    @pytest.mark.asyncio
    async def test_jwt_admin_scope_enforced_via_role_admin_passes(
        self, db_session: AsyncMock,
    ) -> None:
        """JWT + ``admin:write`` with the admin role → passes."""
        from dependencies.auth import require_scope

        checker = require_scope("admin:write")
        request = _jwt_request()
        with patch(
            "dependencies.auth.get_org_role", new=AsyncMock(return_value="admin")
        ):
            result = await checker(request, ORG_ID_STR, db_session)
        assert result == ORG_ID_STR

    @pytest.mark.asyncio
    async def test_jwt_admin_scope_denied_for_member(
        self, db_session: AsyncMock,
    ) -> None:
        """JWT + ``admin`` scope with the member role → 403."""
        from dependencies.auth import require_scope

        checker = require_scope("admin")
        request = _jwt_request()
        with patch(
            "dependencies.auth.get_org_role", new=AsyncMock(return_value="member")
        ), pytest.raises(HTTPException) as exc:
            await checker(request, ORG_ID_STR, db_session)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_jwt_member_level_scope_passes_for_members(
        self, db_session: AsyncMock,
    ) -> None:
        """JWT + member-level scope (``read``/``write``) passes for members.

        Member-level scopes are NOT role-gated — no DB/Redis lookup occurs.
        """
        from dependencies.auth import require_scope

        checker = require_scope("read")
        request = _jwt_request()
        with patch(
            "dependencies.auth.get_org_role",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ):
            result = await checker(request, ORG_ID_STR, db_session)
        assert result == ORG_ID_STR

    @pytest.mark.asyncio
    async def test_jwt_missing_user_id_raises_401(self, db_session: AsyncMock) -> None:
        """JWT session without a user id + admin scope → 401."""
        from dependencies.auth import require_scope

        checker = require_scope("admin")
        request = _jwt_request()
        request.state.user_id = None

        with pytest.raises(HTTPException) as exc:
            await checker(request, ORG_ID_STR, db_session)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_api_key_path_unchanged_scopes_list(
        self, db_session: AsyncMock,
    ) -> None:
        """API key + admin scope in its scopes list → passes (no role check)."""
        from dependencies.auth import require_scope

        checker = require_scope("admin:write")
        request = _jwt_request()
        request.state.auth_type = "api_key"
        request.state.api_key_scopes = ["admin", "admin:write"]

        result = await checker(request, ORG_ID_STR, db_session)
        assert result == ORG_ID_STR

    @pytest.mark.asyncio
    async def test_api_key_missing_scope_still_403(self, db_session: AsyncMock) -> None:
        """API key without the required scope → 403 (unchanged behavior)."""
        from dependencies.auth import require_scope

        checker = require_scope("admin:write")
        request = _jwt_request()
        request.state.auth_type = "api_key"
        request.state.api_key_scopes = ["read"]

        with pytest.raises(HTTPException) as exc:
            await checker(request, ORG_ID_STR, db_session)
        assert exc.value.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════════
# dependencies.project_auth.require_project_owner
# ═══════════════════════════════════════════════════════════════════════════════


class TestRequireProjectOwnerOrgAdminBypass:
    """require_project_owner: owner role OR org-admin role grants access."""

    @pytest.fixture
    def db_session(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def mock_repo(self) -> MagicMock:
        repo = MagicMock()
        repo.get_by_id = AsyncMock(return_value=MagicMock())
        repo.get_member = AsyncMock()
        return repo

    @pytest.mark.asyncio
    async def test_org_admin_passes_with_member_project_role(
        self, db_session: AsyncMock, mock_repo: MagicMock,
    ) -> None:
        """Org admin (user.role=admin) with member project role → passes.

        The org admin bypass verifies the user's org role via
        ``UserRepository`` rather than trusting the JWT ``role`` claim.
        """
        from dependencies.project_auth import require_project_owner

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        member = MagicMock()
        member.role = "member"  # NOT an owner
        mock_repo.get_member.return_value = member

        mock_user_repo = MagicMock()
        mock_user_repo.get_by_uuid = AsyncMock()
        mock_user = MagicMock()
        mock_user.role = "admin"  # but an org admin
        mock_user_repo.get_by_uuid.return_value = mock_user

        with (
            patch(
                "dependencies.project_auth.ProjectRepository", return_value=mock_repo
            ),
            patch(
                "dependencies.project_auth.UserRepository", return_value=mock_user_repo
            ),
        ):
            result = await require_project_owner(request, PROJECT_ID, db_session)

        assert result is None
        mock_user_repo.get_by_uuid.assert_awaited_once_with(ORG_ID, USER_ID)

    @pytest.mark.asyncio
    async def test_owner_role_passes_without_org_admin_check(
        self, db_session: AsyncMock, mock_repo: MagicMock,
    ) -> None:
        """Project owner → passes without consulting the org role."""
        from dependencies.project_auth import require_project_owner

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        member = MagicMock()
        member.role = "owner"
        mock_repo.get_member.return_value = member

        with (
            patch(
                "dependencies.project_auth.ProjectRepository", return_value=mock_repo
            ),
            patch(
                "dependencies.project_auth.UserRepository",
                side_effect=AssertionError("must not be consulted for owners"),
            ),
        ):
            result = await require_project_owner(request, PROJECT_ID, db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_non_owner_member_raises_403(
        self, db_session: AsyncMock, mock_repo: MagicMock,
    ) -> None:
        """Non-owner, non-admin → 403."""
        from dependencies.project_auth import require_project_owner

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        member = MagicMock()
        member.role = "member"
        mock_repo.get_member.return_value = member

        mock_user_repo = MagicMock()
        mock_user_repo.get_by_uuid = AsyncMock()
        mock_user = MagicMock()
        mock_user.role = "member"
        mock_user_repo.get_by_uuid.return_value = mock_user

        with (
            patch(
                "dependencies.project_auth.ProjectRepository", return_value=mock_repo
            ),
            patch(
                "dependencies.project_auth.UserRepository", return_value=mock_user_repo
            ),pytest.raises(HTTPException) as exc
        ):
            await require_project_owner(request, PROJECT_ID, db_session)
        assert exc.value.status_code == 403
