"""Unit tests for the platform-seed repository methods.

Pins the idempotency contract: ``ensure_platform_org`` is a repeatable
``ON CONFLICT DO NOTHING`` under an RLS bypass, and
``create_platform_root`` writes the exact root row (root / superadmin /
verified / no forced rotation) and reports the winner via rowcount.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.organization_repository import OrganizationRepository
from repositories.user_repository import UserRepository

pytestmark = pytest.mark.unit


def _executed_statements(mock_db: AsyncMock) -> list[str]:
    """Render every statement the mocked session executed."""
    return [str(call.args[0]) for call in mock_db.execute.call_args_list]


def _insert_params(mock_db: AsyncMock) -> dict[str, Any]:
    """Return the bound params of the executed INSERT statement."""
    for call in mock_db.execute.call_args_list:
        stmt = call.args[0]
        if "INSERT" in str(stmt).upper():
            return dict(stmt.compile().params)
    raise AssertionError("no INSERT executed")


class TestEnsurePlatformOrg:
    """ensure_platform_org — idempotent, RLS-bypassed platform row."""

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        """Mocked async session — no I/O leaves this process."""
        return AsyncMock(spec=AsyncSession)

    async def test_twice_is_idempotent(self, mock_db: AsyncMock) -> None:
        """Two ensures both succeed — the second is a CONFLICT no-op."""
        repo = OrganizationRepository(db=mock_db)

        await repo.ensure_platform_org()
        await repo.ensure_platform_org()

        inserts = [
            s for s in _executed_statements(mock_db) if "INSERT" in s.upper()
        ]
        assert len(inserts) == 2
        assert all(
            "ON CONFLICT" in s and "DO NOTHING" in s for s in inserts
        )

    async def test_runs_under_rls_bypass(self, mock_db: AsyncMock) -> None:
        """Bypass set/unset wraps the insert — pre-auth safe."""
        repo = OrganizationRepository(db=mock_db)

        await repo.ensure_platform_org()

        statements = _executed_statements(mock_db)
        assert any("app.bypass_rls', 'true'" in s for s in statements)
        assert any("app.bypass_rls', 'false'" in s for s in statements)


class TestPlatformRootRepository:
    """get_platform_root / create_platform_root — bypass + row shape."""

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        """Mocked async session — no I/O leaves this process."""
        return AsyncMock(spec=AsyncSession)

    async def test_get_platform_root_runs_under_rls_bypass(
        self, mock_db: AsyncMock
    ) -> None:
        """Lookup bypasses RLS — works without a tenant context."""
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = result

        root = await UserRepository(db=mock_db).get_platform_root()

        assert root is None
        statements = _executed_statements(mock_db)
        assert any("app.bypass_rls', 'true'" in s for s in statements)
        assert any("app.bypass_rls', 'false'" in s for s in statements)

    async def test_create_platform_root_row_shape(
        self, mock_db: AsyncMock
    ) -> None:
        """Winner insert carries root/superadmin/verified/no-rotation."""
        result = MagicMock()
        result.rowcount = 1
        mock_db.execute.return_value = result

        created = await UserRepository(db=mock_db).create_platform_root(
            "hashed"
        )

        assert created is True
        params = _insert_params(mock_db)
        assert params["external_id"] == "root"
        assert params["role"] == "superadmin"
        assert params["is_email_verified"] is True
        assert params["must_change_password"] is False
        assert params["password_hash"] == "hashed"

    async def test_create_platform_root_loser_returns_false(
        self, mock_db: AsyncMock
    ) -> None:
        """Rowcount zero (lost ON CONFLICT race) → False, no raise."""
        result = MagicMock()
        result.rowcount = 0
        mock_db.execute.return_value = result

        created = await UserRepository(db=mock_db).create_platform_root(
            "hashed"
        )

        assert created is False
