"""Unit tests for is_platform_seeded — the /ready setup probe.

Covers: True when org + root exist, False when either is missing,
None when the check cannot run (fail-open), and None on DB errors.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.health_service import is_platform_seeded

pytestmark = pytest.mark.unit


def _make_factory() -> tuple[MagicMock, AsyncMock]:
    """Build a session factory mock yielding a fresh AsyncMock session."""
    session = AsyncMock()
    context = AsyncMock()
    context.__aenter__.return_value = session
    return MagicMock(return_value=context), session


async def test_seeded_when_org_and_root_exist() -> None:
    """Both platform rows present → True."""
    factory, _ = _make_factory()

    with (
        patch(
            "repositories.organization_repository.OrganizationRepository"
        ) as mock_org_cls,
        patch(
            "repositories.user_repository.UserRepository"
        ) as mock_user_cls,
    ):
        mock_org_cls.return_value.get_platform_org = AsyncMock(
            return_value=object()
        )
        mock_user_cls.return_value.get_platform_root = AsyncMock(
            return_value=object()
        )
        assert await is_platform_seeded(factory) is True


async def test_not_seeded_when_org_missing() -> None:
    """No platform org → False (root lookup skipped)."""
    factory, _ = _make_factory()

    with (
        patch(
            "repositories.organization_repository.OrganizationRepository"
        ) as mock_org_cls,
        patch(
            "repositories.user_repository.UserRepository"
        ) as mock_user_cls,
    ):
        mock_org_cls.return_value.get_platform_org = AsyncMock(
            return_value=None
        )
        mock_user_cls.return_value.get_platform_root = AsyncMock(
            return_value=None
        )
        assert await is_platform_seeded(factory) is False
        mock_user_cls.return_value.get_platform_root.assert_not_awaited()


async def test_not_seeded_when_root_missing() -> None:
    """Org present but root missing → False."""
    factory, _ = _make_factory()

    with (
        patch(
            "repositories.organization_repository.OrganizationRepository"
        ) as mock_org_cls,
        patch(
            "repositories.user_repository.UserRepository"
        ) as mock_user_cls,
    ):
        mock_org_cls.return_value.get_platform_org = AsyncMock(
            return_value=object()
        )
        mock_user_cls.return_value.get_platform_root = AsyncMock(
            return_value=None
        )
        assert await is_platform_seeded(factory) is False


async def test_none_when_factory_unavailable() -> None:
    """No session factory (startup bypassed in tests) → None."""
    assert await is_platform_seeded(None) is None


async def test_none_on_db_error() -> None:
    """DB failure → None (fail-open, /ready must not claim setup)."""
    factory = MagicMock(side_effect=RuntimeError("db down"))

    assert await is_platform_seeded(factory) is None
