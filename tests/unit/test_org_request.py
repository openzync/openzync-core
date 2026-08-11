"""Unit tests for OrgRequestService — the in-app org-creation channel.

All external IO (system config, org service, auth service, OTP) is mocked
at the service boundary.

Observed contract:
- reject_all                      → 403, nothing created.
- approvals WITHOUT in_app scope  → 403.
- allow_all                       → org created instantly + OTP admin.
- approvals + in_app scope        → pending org + pending admin, no OTP.
- duplicate admin email (pending) → ConflictError (409).
- reserved SYSTEM name            → ValidationError (422) at schema level.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from core.exceptions import AuthorizationError, ConflictError
from schemas.org_requests import OrgRequestCreate
from schemas.organizations import CreateOrgResponse
from schemas.system_config import SystemConfigResponse
from services.org_request_service import OrgRequestService

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


def _payload(
    name: str = "Acme Corp",
    email: str = "admin@acme.com",
    admin_name: str | None = "Admin",
) -> OrgRequestCreate:
    """Build a valid org-request payload."""
    return OrgRequestCreate(
        organization_name=name,
        admin_email=email,
        admin_name=admin_name,
    )


def _policy(policy: str = "allow_all", scope: str = "both") -> SystemConfigResponse:
    """Build a system config for a given policy/scope."""
    return SystemConfigResponse(
        org_creation_policy=policy,
        approval_scope=scope,
    )


class TestOrgRequestService:
    """OrgRequestService.request_org_creation unit tests."""

    @pytest.fixture
    def service(self) -> tuple[OrgRequestService, dict[str, AsyncMock]]:
        """Build the service with fully mocked collaborators."""
        mocks = {
            "auth_service": AsyncMock(),
            "org_service": AsyncMock(),
            "otp_service": AsyncMock(),
            "redis": AsyncMock(),
            "bao_client": AsyncMock(),
        }
        svc = OrgRequestService(
            db=AsyncMock(),
            auth_service=mocks["auth_service"],
            org_service=mocks["org_service"],
            otp_service=mocks["otp_service"],
            redis=mocks["redis"],
            bao_client=mocks["bao_client"],
        )
        return svc, mocks

    @pytest.mark.asyncio
    async def test_reject_all_raises_403(
        self, service: tuple[OrgRequestService, dict[str, AsyncMock]]
    ) -> None:
        """reject_all → AuthorizationError, nothing is created."""
        svc, mocks = service
        with patch(
            "core.system_config.get_system_config",
            new=AsyncMock(return_value=_policy(policy="reject_all")),
        ), pytest.raises(AuthorizationError):
            await svc.request_org_creation(_payload())

        mocks["org_service"].create_organization.assert_not_awaited()
        mocks["auth_service"].create_pending_org_and_admin.assert_not_awaited()
        mocks["otp_service"].generate_and_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approvals_without_in_app_scope_raises_403(
        self, service: tuple[OrgRequestService, dict[str, AsyncMock]]
    ) -> None:
        """approvals without in_app scope → 403 (this channel is not gated)."""
        svc, mocks = service
        with patch(
            "core.system_config.get_system_config",
            new=AsyncMock(
                return_value=_policy(policy="approvals", scope="public_signup"),
            ),
        ), pytest.raises(AuthorizationError):
            await svc.request_org_creation(_payload())

        mocks["org_service"].create_organization.assert_not_awaited()
        mocks["auth_service"].create_pending_org_and_admin.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allow_all_creates_org_instantly_with_otp_admin(
        self, service: tuple[OrgRequestService, dict[str, AsyncMock]]
    ) -> None:
        """allow_all → live org + OTP-activated admin, status approved."""
        svc, mocks = service
        # The service's own AuthRepository runs over the mocked db — stub
        # the duplicate-email check to report no existing live user.
        svc._auth_repo = AsyncMock()  # noqa: SLF001
        svc._auth_repo.find_user_by_email.return_value = None  # noqa: SLF001
        mocks["org_service"].create_organization.return_value = CreateOrgResponse(
            organization_id=ORG_ID,
            organization_name="Acme Corp",
        )

        result = await svc.request_org_creation(_payload())

        assert result.status == "approved"
        assert result.organization_name == "Acme Corp"
        assert result.admin_email == "admin@acme.com"

        mocks["org_service"].create_organization.assert_awaited_once()
        mocks["auth_service"].create_live_admin_user.assert_awaited_once_with(
            organization_id=ORG_ID,
            email="admin@acme.com",
            name="Admin",
        )
        mocks["otp_service"].generate_and_send.assert_awaited_once_with(
            email="admin@acme.com", purpose="signup"
        )
        mocks["auth_service"].create_pending_org_and_admin.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approvals_in_app_scope_creates_pending(
        self, service: tuple[OrgRequestService, dict[str, AsyncMock]]
    ) -> None:
        """approvals + in_app scope → pending org + pending admin, no OTP."""
        svc, mocks = service
        # The service's own AuthRepository runs over the mocked db — stub
        # the duplicate-email check to report no existing live user.
        svc._auth_repo = AsyncMock()  # noqa: SLF001
        svc._auth_repo.find_user_by_email.return_value = None  # noqa: SLF001

        with patch(
            "core.system_config.get_system_config",
            new=AsyncMock(
                return_value=_policy(policy="approvals", scope="in_app"),
            ),
        ):
            result = await svc.request_org_creation(_payload())

        assert result.status == "pending"
        mocks["auth_service"].create_pending_org_and_admin.assert_awaited_once_with(
            organization_name="Acme Corp",
            admin_email="admin@acme.com",
            admin_name="Admin",
        )
        mocks["org_service"].create_organization.assert_not_awaited()
        mocks["otp_service"].generate_and_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approvals_both_scope_creates_pending(
        self, service: tuple[OrgRequestService, dict[str, AsyncMock]]
    ) -> None:
        """approvals with 'both' scope also gates the in-app channel."""
        svc, mocks = service
        svc._auth_repo = AsyncMock()  # noqa: SLF001
        svc._auth_repo.find_user_by_email.return_value = None  # noqa: SLF001
        with patch(
            "core.system_config.get_system_config",
            new=AsyncMock(return_value=_policy(policy="approvals", scope="both")),
        ):
            result = await svc.request_org_creation(_payload())

        assert result.status == "pending"
        mocks["auth_service"].create_pending_org_and_admin.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_admin_email_raises_conflict(
        self, service: tuple[OrgRequestService, dict[str, AsyncMock]]
    ) -> None:
        """A live account with the admin email → 409 ConflictError.

        The pending path requires a globally-unique admin email (the
        ix_user_email_unique index).  The service checks first and raises
        409 with a clear message.
        """
        svc, mocks = service
        existing = AsyncMock(id=UUID("00000000-0000-0000-0000-000000000099"))
        svc._auth_repo = AsyncMock()  # noqa: SLF001
        svc._auth_repo.find_user_by_email.return_value = existing  # noqa: SLF001

        with patch(
            "core.system_config.get_system_config",
            new=AsyncMock(
                return_value=_policy(policy="approvals", scope="in_app"),
            ),
        ), pytest.raises(ConflictError):
            await svc.request_org_creation(_payload())

        mocks["auth_service"].create_pending_org_and_admin.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_admin_email_integrity_error_maps_to_409(
        self, service: tuple[OrgRequestService, dict[str, AsyncMock]]
    ) -> None:
        """Concurrent duplicate (IntegrityError) → 409 with clear message."""
        from sqlalchemy.exc import IntegrityError

        svc, mocks = service
        svc._auth_repo = AsyncMock()  # noqa: SLF001
        svc._auth_repo.find_user_by_email.return_value = None  # noqa: SLF001
        mocks["auth_service"].create_pending_org_and_admin.side_effect = (
            IntegrityError("stmt", {}, Exception("unique"))
        )

        with patch(
            "core.system_config.get_system_config",
            new=AsyncMock(
                return_value=_policy(policy="approvals", scope="in_app"),
            ),
        ), pytest.raises(ConflictError):
            await svc.request_org_creation(_payload())

    @pytest.mark.asyncio
    async def test_allow_all_duplicate_admin_email_raises_conflict_before_create(
        self, service: tuple[OrgRequestService, dict[str, AsyncMock]]
    ) -> None:
        """allow_all: a live account with the admin email → 409, nothing created."""
        svc, mocks = service
        existing = AsyncMock(id=UUID("00000000-0000-0000-0000-000000000099"))
        svc._auth_repo = AsyncMock()  # noqa: SLF001
        svc._auth_repo.find_user_by_email.return_value = existing  # noqa: SLF001

        with patch(
            "core.system_config.get_system_config",
            new=AsyncMock(return_value=_policy(policy="allow_all")),
        ), pytest.raises(ConflictError):
            await svc.request_org_creation(_payload())

        mocks["org_service"].create_organization.assert_not_awaited()
        mocks["auth_service"].create_live_admin_user.assert_not_awaited()
        mocks["otp_service"].generate_and_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allow_all_duplicate_integrity_error_maps_to_409(
        self, service: tuple[OrgRequestService, dict[str, AsyncMock]]
    ) -> None:
        """allow_all: concurrent duplicate (IntegrityError) → 409, transaction rolled back."""
        from sqlalchemy.exc import IntegrityError

        svc, mocks = service
        svc._auth_repo = AsyncMock()  # noqa: SLF001
        svc._auth_repo.find_user_by_email.return_value = None  # noqa: SLF001
        mocks["auth_service"].create_live_admin_user.side_effect = IntegrityError(
            "stmt", {}, Exception("unique")
        )

        with patch(
            "core.system_config.get_system_config",
            new=AsyncMock(return_value=_policy(policy="allow_all")),
        ), pytest.raises(ConflictError):
            await svc.request_org_creation(_payload())

        svc._auth_repo.rollback.assert_awaited_once()  # noqa: SLF001


class TestOrgRequestSchema:
    """OrgRequestCreate schema-level validation."""

    def test_reserved_system_name_rejected(self) -> None:
        """The reserved SYSTEM name (any case) → ValidationError (422)."""
        from pydantic import ValidationError

        for bad in ("SYSTEM", "system", "System"):
            with pytest.raises(ValidationError):
                _payload(name=bad)

    def test_blank_name_rejected(self) -> None:
        """Whitespace-only org names → ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _payload(name="   ")
