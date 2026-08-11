"""Org-request service — the in-app organization creation channel.

Any authenticated dashboard user may request a new organization via
``POST /v1/org-requests``.  The platform ``org_creation_policy`` decides
the outcome:

- ``reject_all`` → 403, nothing is created.
- ``allow_all`` → the org is created instantly with the designated admin
  activated by email OTP.
- ``approvals`` — gated by ``approval_scope``:
  - ``in_app`` scope (or ``both``) → pending org + pending admin user,
    awaiting superadmin approval (``OrganizationService.approve_org``).
  - without ``in_app`` scope → 403 (this channel is not gated).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from core.exceptions import AuthorizationError, ConflictError
from repositories.auth_repository import AuthRepository
from schemas.org_requests import OrgRequestCreate, OrgRequestResponse
from schemas.organizations import CreateOrgRequest

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis
    from sqlalchemy.ext.asyncio import AsyncSession

    from core.openbao import OpenBaoClient
    from schemas.system_config import SystemConfigResponse
    from services.auth_service import AuthService
    from services.organization_service import OrganizationService
    from services.otp_service import OtpService

logger = logging.getLogger(__name__)


class OrgRequestService:
    """Business logic for in-app organization creation requests.

    Args:
        db: Request-scoped async DB session (shared with the auth service).
        auth_service: Auth service — shared pending-org/admin creation and
            live-admin-user creation.
        org_service: Organization service — instant org creation with
            prompt seeding + OpenBao bootstrap.
        otp_service: OTP service — email activation for the allow_all path.
        redis: Async Redis client (system-config cache).
        bao_client: Authenticated OpenBao client (system-config source).
    """

    def __init__(
        self,
        db: AsyncSession,  # noqa: F821
        auth_service: AuthService,  # noqa: F821
        org_service: OrganizationService,  # noqa: F821
        otp_service: OtpService,  # noqa: F821
        redis: AsyncRedis,  # noqa: F821
        bao_client: OpenBaoClient | None,  # noqa: F821
    ) -> None:
        self._db = db
        self._auth_repo = AuthRepository(db)
        self._auth_service = auth_service
        self._org_service = org_service
        self._otp_service = otp_service
        self._redis = redis
        self._bao_client = bao_client

    async def request_org_creation(
        self,
        payload: OrgRequestCreate,
    ) -> OrgRequestResponse:
        """Create (or queue) an organization per the platform policy.

        Args:
            payload: Organization name + designated admin email/name.  The
                schema already rejects the reserved ``SYSTEM`` name.

        Returns:
            An ``OrgRequestResponse`` — ``status='approved'`` when the org
            was created instantly, ``status='pending'`` when it awaits
            superadmin approval.

        Raises:
            AuthorizationError: If the platform policy is ``reject_all``,
                or ``approvals`` is active without ``in_app`` scope.
            ConflictError: If the designated admin email already belongs to
                a live account (in the pending path).
        """
        # Lazy import — avoid import-time cycles with the auth service.
        from core.system_config import get_system_config

        system_config = await get_system_config(
            self._redis, self._bao_client
        )
        if system_config.org_creation_policy == "reject_all":
            raise AuthorizationError("Registration is disabled")

        if system_config.org_creation_policy == "allow_all":
            return await self._create_instantly(system_config, payload)

        # approvals policy — gated by the in_app scope
        if system_config.approval_scope not in ("in_app", "both"):
            raise AuthorizationError(
                "New organization requests are not accepted at this time"
            )
        return await self._create_pending(system_config, payload)

    # ── Branch implementations ─────────────────────────────────────────────

    async def _create_instantly(
        self,
        system_config: SystemConfigResponse,  # noqa: ARG001 — policy context for audit
        payload: OrgRequestCreate,
    ) -> OrgRequestResponse:
        """allow_all: create a live org + OTP-activated admin."""
        org = await self._org_service.create_organization(
            CreateOrgRequest(name=payload.organization_name, plan="free")
        )
        await self._auth_service.create_live_admin_user(
            organization_id=org.organization_id,
            email=str(payload.admin_email),
            name=payload.admin_name,
        )
        # OTP email — the admin activates via verify-email, then sets a
        # real password via the reset flow (they have none yet).
        await self._otp_service.generate_and_send(
            email=str(payload.admin_email),
            purpose="signup",
        )
        logger.info(
            "org_request.created",
            extra={
                "org_id": str(org.organization_id),
                "admin_email": str(payload.admin_email),
                "policy": "allow_all",
            },
        )
        return OrgRequestResponse(
            organization_name=org.organization_name,
            admin_email=payload.admin_email,
            status="approved",
        )

    async def _create_pending(
        self,
        system_config: SystemConfigResponse,  # noqa: ARG001 — policy context for audit
        payload: OrgRequestCreate,
    ) -> OrgRequestResponse:
        """Approvals + in_app scope: queue a pending org + pending admin."""
        existing = await self._auth_repo.find_user_by_email(str(payload.admin_email))
        if existing is not None:
            raise ConflictError(
                f"An account with email '{payload.admin_email}' already exists. "
                "Each organization requires a unique admin email."
            )
        try:
            await self._auth_service.create_pending_org_and_admin(
                organization_name=payload.organization_name,
                admin_email=str(payload.admin_email),
                admin_name=payload.admin_name or str(payload.admin_email).split("@")[0],
            )
        except IntegrityError:
            # Concurrent duplicate — the unique email index won.  Roll back
            # the aborted transaction so the session stays usable.
            await self._auth_repo.rollback()
            raise ConflictError(
                f"An account with email '{payload.admin_email}' already exists. "
                "Each organization requires a unique admin email."
            ) from None

        logger.info(
            "org_request.pending",
            extra={"admin_email": str(payload.admin_email), "policy": "approvals"},
        )
        return OrgRequestResponse(
            organization_name=payload.organization_name,
            admin_email=payload.admin_email,
            status="pending",
        )
