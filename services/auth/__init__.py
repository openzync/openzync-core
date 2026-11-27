"""Auth service — facade composing focused authentication collaborators.

Sub-package layout (one responsibility per module)::

    registration.py  singleton-org signup (no org creation)
    org_join.py      singleton-org member signup + email verification
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
from repositories.instance_settings_repository import InstanceSettingsRepository
from services.auth.org_join import OrgJoinMixin
from services.auth.password import PasswordMixin
from services.auth.passwordless import PasswordlessMixin
from services.auth.profile import ProfileMixin
from services.auth.registration import RegistrationMixin
from services.auth.session import SessionMixin
from services.auth.token import TokenMixin

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis

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
        email_service: Optional email service for notification-only emails
            (e.g. password-change confirmation).  ``None`` skips notifications.
        settings_repo: Singleton settings row (registration mode).
            Injected via ``dependencies.services.get_auth_service``.
    """

    def __init__(
        self,
        repo: AuthRepository,
        otp_service: OtpService,
        redis: AsyncRedis,
        email_service: EmailService | None = None,
        settings_repo: InstanceSettingsRepository | None = None,
    ) -> None:
        self._repo = repo
        self._otp_service = otp_service
        self._redis = redis
        self._email_service = email_service
        self._settings_repo = settings_repo


__all__ = ["AuthService"]
