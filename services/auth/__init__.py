"""Auth service — facade composing focused authentication collaborators.

Sub-package layout (one responsibility per module)::

    registration.py  signup + pending-org/admin creation
    org_join.py      org-code join + email verification
    password.py      change / forgot / reset + strength check
    passwordless.py  email-OTP login
    session.py       login + MFA verify/enable/disable
    token.py         refresh rotation + token issuance
    profile.py       dashboard profile read/update

``AuthService`` keeps the original import path and public API; routers
and factories are unchanged. ``services/auth_service.py`` re-exports it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from repositories.auth_repository import AuthRepository
from repositories.organization_repository import OrganizationRepository
from services.auth.org_join import OrgJoinMixin
from services.auth.password import PasswordMixin
from services.auth.passwordless import PasswordlessMixin
from services.auth.profile import ProfileMixin
from services.auth.registration import RegistrationMixin
from services.auth.session import SessionMixin
from services.auth.token import TokenMixin

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis

    from core.openbao import OpenBaoClient
    from services.email_service import EmailService
    from services.otp_service import OtpService

logger = logging.getLogger(__name__)


class AuthService(
    RegistrationMixin,
    OrgJoinMixin,
    PasswordMixin,
    PasswordlessMixin,
    SessionMixin,
    TokenMixin,
    ProfileMixin,
):
    """Handles dashboard authentication flows.

    Args:
        repo: Repository for auth-related DB access.
        otp_service: OTP service for email verification and MFA.
        redis: Async Redis client for MFA session storage.
        org_repo: Repository for organization lookups (org-code join).
        email_service: Optional email service for notification-only emails
            (e.g. password-change confirmation).  ``None`` skips notifications.
        bao_client: Optional OpenBao client for bootstrapping org namespaces
            during email verification.
    """

    def __init__(
        self,
        repo: AuthRepository,
        otp_service: OtpService,
        redis: AsyncRedis,
        org_repo: OrganizationRepository,
        email_service: EmailService | None = None,
        bao_client: OpenBaoClient | None = None,
    ) -> None:
        self._repo = repo
        self._otp_service = otp_service
        self._redis = redis
        self._org_repo = org_repo
        self._email_service = email_service
        self._bao_client = bao_client


__all__ = ["AuthService"]
