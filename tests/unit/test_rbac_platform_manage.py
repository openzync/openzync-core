"""Unit tests for the ``platform:manage`` RBAC contract.

Pins the permission vocabulary and the holder gate in ``core.rbac``:

- ``platform:manage`` is part of ``ALL_PERMISSIONS``;
- ``MEMBER_DEFAULT_PERMISSIONS`` excludes ``members:*``/``platform:*``;
- ``has_platform_manage`` grants admins by wildcard, explicit holders
  by grant, and fails closed for plain members and unknown users.

Role/permission lookups are mocked at the ``core.rbac`` module boundary
— no Redis, no DB.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from core.rbac import (
    ALL_PERMISSIONS,
    MEMBER_DEFAULT_PERMISSIONS,
    has_platform_manage,
)

USER_ID = UUID("00000000-0000-0000-0000-000000000002")


def test_platform_manage_in_all_permissions() -> None:
    """``platform:manage`` is part of the permission vocabulary."""
    assert "platform:manage" in ALL_PERMISSIONS


def test_member_defaults_exclude_members_and_platform() -> None:
    """New members get no ``members:*`` or ``platform:*`` grants."""
    assert not any(
        p.startswith("members:") or p.startswith("platform:")
        for p in MEMBER_DEFAULT_PERMISSIONS
    )
    assert set(MEMBER_DEFAULT_PERMISSIONS) == {
        p
        for p in ALL_PERMISSIONS
        if not (p.startswith("members:") or p.startswith("platform:"))
    }


@pytest.mark.asyncio
async def test_has_platform_manage_admin_wildcard_true() -> None:
    """Admins hold everything via wildcard, even with empty grants."""
    with (
        patch("core.rbac.get_user_role", new=AsyncMock(return_value="admin")),
        patch(
            "core.rbac.get_effective_permissions",
            new=AsyncMock(return_value=frozenset()),
        ),
    ):
        assert await has_platform_manage(AsyncMock(), AsyncMock(), USER_ID) is True


@pytest.mark.asyncio
async def test_has_platform_manage_explicit_grant_true() -> None:
    """A member with the explicit grant is a holder."""
    with (
        patch("core.rbac.get_user_role", new=AsyncMock(return_value="member")),
        patch(
            "core.rbac.get_effective_permissions",
            new=AsyncMock(
                return_value=frozenset({"project:read", "platform:manage"})
            ),
        ),
    ):
        assert await has_platform_manage(AsyncMock(), AsyncMock(), USER_ID) is True


@pytest.mark.asyncio
async def test_has_platform_manage_plain_member_false() -> None:
    """A member with only default grants is not a holder."""
    with (
        patch("core.rbac.get_user_role", new=AsyncMock(return_value="member")),
        patch(
            "core.rbac.get_effective_permissions",
            new=AsyncMock(return_value=frozenset(MEMBER_DEFAULT_PERMISSIONS)),
        ),
    ):
        assert (
            await has_platform_manage(AsyncMock(), AsyncMock(), USER_ID) is False
        )


@pytest.mark.asyncio
async def test_has_platform_manage_unknown_user_fail_closed() -> None:
    """Unknown users degrade to member/empty — denied, never granted."""
    with (
        patch("core.rbac.get_user_role", new=AsyncMock(return_value="member")),
        patch(
            "core.rbac.get_effective_permissions",
            new=AsyncMock(return_value=frozenset()),
        ),
    ):
        assert (
            await has_platform_manage(AsyncMock(), AsyncMock(), USER_ID) is False
        )
