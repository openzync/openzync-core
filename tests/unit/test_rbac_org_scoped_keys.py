"""RBAC org-scoped role cache keys (mocked repos/Redis, no I/O)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

pytestmark = pytest.mark.unit

ORG_A = UUID("00000000-0000-0000-0000-0000000000a1")
ORG_B = UUID("00000000-0000-0000-0000-0000000000b2")
USER_ID = UUID("00000000-0000-0000-0000-000000000001")


class _FakeRedis:
    """Minimal dict-backed async Redis for cache-key isolation tests."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def setex(self, key: str, _ttl: int, value: str) -> bool:
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = sum(1 for k in keys if self.store.pop(k, None) is not None)
        return removed


def _user_with_role(role: str) -> MagicMock:
    user = MagicMock()
    user.is_active = True
    user.is_deleted = False
    user.role = role
    user.permissions = []
    return user


class TestRbacOrgScopedKeys:
    """RBAC: roles are cached per ``(org_id, user_id)``, never per user."""

    @pytest.mark.asyncio
    async def test_two_org_sequence_returns_each_org_role(self) -> None:
        """Admin in A, member in B → B lookup returns member, not A's admin."""
        from core import rbac

        redis = _FakeRedis()
        roles = {ORG_A: "admin", ORG_B: "member"}

        def _repo(_db: object) -> MagicMock:
            repo = MagicMock()

            async def _get_by_uuid(org_id: UUID, _user_id: UUID) -> MagicMock:
                return _user_with_role(roles[org_id])

            repo.get_by_uuid.side_effect = _get_by_uuid
            return repo

        with patch.object(rbac, "UserRepository", side_effect=_repo):
            role_a = await rbac.get_org_role(redis, AsyncMock(), ORG_A, USER_ID)  # type: ignore[arg-type]
            role_b = await rbac.get_org_role(redis, AsyncMock(), ORG_B, USER_ID)  # type: ignore[arg-type]

        assert role_a == "admin"
        assert role_b == "member"
        # Both org-scoped keys exist independently in the cache.
        assert rbac._role_cache_key(ORG_A, USER_ID) != rbac._role_cache_key(ORG_B, USER_ID)
        assert len(redis.store) == 2
