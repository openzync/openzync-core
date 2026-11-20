"""Unit tests for SetupService — one-time platform root bootstrap.

Covers: fresh-platform happy path (org ensured + root inserted),
already-set-up conflict, weak-password validation via the shared
validator, the lost ``ON CONFLICT`` race (insert returns False), and
unexpected DB errors propagating unwrapped.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.exceptions import ConflictError, ValidationError
from repositories.organization_repository import OrganizationRepository
from repositories.user_repository import UserRepository
from services.setup_service import SetupService

pytestmark = pytest.mark.unit

STRONG_PASSWORD = "StrongPass1"  # noqa: S106 — test fixture password


class TestSetupService:
    """SetupService.create_root_once — no I/O, repos mocked."""

    @pytest.fixture
    def mock_user_repo(self) -> AsyncMock:
        """User repository mock for root lookups and inserts."""
        return AsyncMock(spec=UserRepository)

    @pytest.fixture
    def mock_org_repo(self) -> AsyncMock:
        """Organization repository mock for platform-org ensure."""
        return AsyncMock(spec=OrganizationRepository)

    @pytest.fixture
    def service(
        self, mock_user_repo: AsyncMock, mock_org_repo: AsyncMock
    ) -> SetupService:
        """SetupService wired to the mocked repositories."""
        return SetupService(
            user_repo=mock_user_repo, org_repo=mock_org_repo
        )

    async def test_happy_path_ensures_org_and_creates_root(
        self,
        service: SetupService,
        mock_user_repo: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> None:
        """Fresh platform → org ensured, root inserted, no error."""
        mock_user_repo.create_platform_root.return_value = True

        with patch(
            "services.setup_service.hash_password",
            return_value="hashed",
        ) as mock_hash:
            await service.create_root_once(STRONG_PASSWORD)

        mock_org_repo.ensure_platform_org.assert_awaited_once_with(
            name="Platform"
        )
        mock_hash.assert_called_once_with(STRONG_PASSWORD)
        mock_user_repo.create_platform_root.assert_awaited_once_with(
            "hashed"
        )

    async def test_org_name_applies_on_first_creation(
        self,
        service: SetupService,
        mock_user_repo: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> None:
        """Custom org name is forwarded to the idempotent ensure."""
        mock_user_repo.create_platform_root.return_value = True

        with patch(
            "services.setup_service.hash_password",
            return_value="hashed",
        ):
            await service.create_root_once(STRONG_PASSWORD, "Acme Corp")

        mock_org_repo.ensure_platform_org.assert_awaited_once_with(
            name="Acme Corp"
        )

    async def test_root_exists_raises_conflict(
        self,
        service: SetupService,
        mock_user_repo: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> None:
        """Insert reports no row (already set up) → generic ConflictError."""
        mock_user_repo.create_platform_root.return_value = False

        with (
            patch(
                "services.setup_service.hash_password",
                return_value="hashed",
            ),
            pytest.raises(ConflictError, match="not available"),
        ):
            await service.create_root_once(STRONG_PASSWORD)

        mock_user_repo.create_platform_root.assert_awaited_once()

    async def test_weak_password_raises_validation_error(
        self,
        service: SetupService,
        mock_user_repo: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> None:
        """Weak password → shared-validator ValidationError, no writes."""
        with pytest.raises(ValidationError, match="uppercase"):
            await service.create_root_once("weakpass1")

        mock_org_repo.ensure_platform_org.assert_not_awaited()
        mock_user_repo.create_platform_root.assert_not_awaited()

    async def test_blank_org_name_raises_validation_error(
        self,
        service: SetupService,
        mock_user_repo: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> None:
        """Blank org name → ValidationError, no writes."""
        with pytest.raises(ValidationError, match="blank"):
            await service.create_root_once(STRONG_PASSWORD, "   ")

        mock_org_repo.ensure_platform_org.assert_not_awaited()
        mock_user_repo.create_platform_root.assert_not_awaited()

    async def test_lost_race_raises_conflict(
        self,
        service: SetupService,
        mock_user_repo: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> None:
        """Insert returns False (lost ON CONFLICT race) → ConflictError."""
        mock_user_repo.create_platform_root.return_value = False

        with (
            patch(
                "services.setup_service.hash_password",
                return_value="hashed",
            ),
            pytest.raises(ConflictError, match="not available"),
        ):
            await service.create_root_once(STRONG_PASSWORD)

        mock_org_repo.ensure_platform_org.assert_awaited_once()

    async def test_db_error_propagates(
        self,
        service: SetupService,
        mock_org_repo: AsyncMock,
    ) -> None:
        """Unexpected DB failure surfaces unwrapped — never masked."""
        mock_org_repo.ensure_platform_org.side_effect = RuntimeError(
            "db down"
        )

        with pytest.raises(RuntimeError, match="db down"):
            await service.create_root_once(STRONG_PASSWORD)
