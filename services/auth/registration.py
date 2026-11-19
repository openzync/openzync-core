"""Auth registration — signup into the singleton platform org.

Single platform org: signup creates a dashboard user inside the existing
platform org — no org is ever created here.  Registration is governed by
``instance_settings.registration_mode``:

- ``open`` → the user is created live (role ``admin``) and sent an OTP.
- ``invite_only`` → 403 (the user must arrive via an admin invite).
- ``approval`` → the user is created inactive (``is_active=False``) and
  the response is ``status='pending'`` — an admin activates them later.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    AuthorizationError,
)
from repositories.instance_settings_repository import InstanceSettingsRepository
from schemas.auth import (
    PendingOrgResponse,
    SignupRequest,
    SignupResponse,
)

logger = logging.getLogger(__name__)


class RegistrationMixin:
    """Composed into :class:`AuthService`."""

    _repo: Any
    _otp_service: Any
    _redis: Any
    _org_repo: Any
    _email_service: Any
    _bao_client: Any

    async def _registration_mode(self) -> str:
        """Return the platform registration mode (defaults ``invite_only``).

        Args:
            None — reads the singleton ``instance_settings`` row.

        Returns:
            One of ``open``, ``invite_only``, ``approval``.
        """
        row = await InstanceSettingsRepository(self._org_repo.session).get()
        return row.registration_mode or "invite_only"

    # ── Signup ──────────────────────────────────────────────────────────────

    async def signup(
        self, payload: SignupRequest
    ) -> SignupResponse | PendingOrgResponse:
        """Create a dashboard user in the singleton platform org.

        The response is identical whether or not the email is already
        registered — the real distinction is logged server-side, so the
        endpoint cannot be used to enumerate existing accounts.

        Args:
            payload: Signup request with email and password only.

        Returns:
            A ``SignupResponse`` with a confirmation message (``open``
            mode), or a ``PendingOrgResponse`` when the platform requires
            approval.

        Raises:
            AuthorizationError: If registration is ``invite_only`` or the
                platform org is missing (misconfigured bootstrap).
            ValidationError: If the password does not meet requirements.
        """
        from services import auth_service as _facade  # via facade (patchable)

        mode = await self._registration_mode()
        if mode == "invite_only":
            raise AuthorizationError("Registration is invite-only")
        if mode not in ("open", "approval"):
            raise AuthorizationError("Registration is disabled")

        # Validate password strength
        self._validate_password(payload.password)

        # Check email uniqueness
        existing = await self._repo.find_user_by_email(payload.email)
        if existing is not None:
            logger.warning(
                "security.signup_existing_email",
                extra={"email": payload.email},
            )
            return self._signup_success_response(payload.email)

        org = await self._org_repo.get_platform_org()
        if org is None:
            raise AuthorizationError("Registration is disabled")
        await self._org_repo.set_tenant_context(org.id)

        try:
            pw_hash = _facade.hash_password(payload.password)
            await self._repo.create_dashboard_user(
                organization_id=org.id,
                email=payload.email,
                password_hash=pw_hash,
                name=payload.email.split("@")[0],
                role="admin" if mode == "open" else "member",
                permissions=[],
            )
        except IntegrityError:
            # Concurrent duplicate signup — the unique email index won.
            # Same generic response as a fresh signup; roll back the aborted
            # transaction so the session stays usable.
            logger.warning(
                "security.signup_existing_email",
                extra={"email": payload.email},
            )
            await self._repo.rollback()
            return self._signup_success_response(payload.email)

        if mode == "approval":
            await self._deactivate_pending(payload.email)
            return PendingOrgResponse(
                message=(
                    "Your account is pending approval. "
                    "You will receive an email when it is approved."
                ),
            )

        await self._otp_service.generate_and_send(
            email=payload.email,
            purpose="signup",
        )
        return self._signup_success_response(payload.email)

    async def _deactivate_pending(self, email: str) -> None:
        """Deactivate a just-created approval-mode user.

        Args:
            email: The user's email address.
        """
        user = await self._repo.find_user_by_email(email)
        if user is not None:
            user.is_active = False
            await self._repo.flush()
