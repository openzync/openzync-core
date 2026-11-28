"""Unit tests for the platform:manage gate in quick actions.

``QuickActionsService.get_actions`` omits (not 403s) the privileged
``/users`` and ``/audit`` links for callers without ``platform:manage``
(admins hold it by wildcard).  Repositories and config resolution are
mocked — no DB, no Redis.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from services.quick_actions_service import QuickActionsService

USER_ID = UUID("00000000-0000-0000-0000-000000000002")


def _service(
    *,
    role: str,
    permissions: list[str],
    user_count: int = 1,
) -> QuickActionsService:
    """Service with mocked repos/config for one caller identity."""
    project_repo = AsyncMock()
    project_repo.count_active = AsyncMock(return_value=3)
    user_repo = AsyncMock()
    user_repo.count_active = AsyncMock(return_value=user_count)
    user_repo.get_by_uuid = AsyncMock(
        return_value=SimpleNamespace(role=role, permissions=permissions)
    )
    org_repo = AsyncMock()
    return QuickActionsService(
        project_repo=project_repo,
        user_repo=user_repo,
        org_repo=org_repo,
        db=AsyncMock(),
        redis=None,
    )


@pytest.mark.asyncio
async def test_holder_sees_users_and_audit_links() -> None:
    """A ``platform:manage`` holder is offered /users and /audit."""
    service = _service(role="member", permissions=["platform:manage"])
    with patch(
        "services.quick_actions_service.get_platform_config",
        new=AsyncMock(return_value=SimpleNamespace(llm_backend="openai")),
    ):
        actions = await service.get_actions(USER_ID)
    hrefs = [a.href for a in actions]
    assert "/users" in hrefs
    assert "/audit" in hrefs


@pytest.mark.asyncio
async def test_non_holder_sees_neither() -> None:
    """A plain member gets no privileged links — but keeps the basics."""
    service = _service(
        role="member",
        permissions=["project:read", "project:write"],
    )
    with patch(
        "services.quick_actions_service.get_platform_config",
        new=AsyncMock(return_value=SimpleNamespace(llm_backend="openai")),
    ):
        actions = await service.get_actions(USER_ID)
    hrefs = [a.href for a in actions]
    assert "/users" not in hrefs
    assert "/audit" not in hrefs
    assert "/projects" in hrefs
    assert "/overview" in hrefs
