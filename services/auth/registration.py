"""Auth registration — signup into the singleton platform org.

Single platform org: signup creates a dashboard user inside the existing
platform org — no org is ever created here.  Registration is governed by
``instance_settings.registration_mode``:

- ``open`` → the user is created live (role ``member``) and sent an OTP.
- ``invite_only`` → 403 (the user must arrive via an admin invite).
- ``approval`` → the user is created inactive (``is_active=False``) and
  the response is ``status='pending'`` — an admin activates them later.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.exc import IntegrityError

from core.rbac import MEMBER_DEFAULT_PERMISSIONS
from core.rls import set_user_context
from repositories.instance_settings_repository import InstanceSettingsRepository
from schemas.auth import (
    PendingOrgResponse,
    SignupRequest,
    SignupResponse,
)
from utils.pii import mask_email

logger = logging.getLogger(__name__)


class RegistrationMixin:
    """Composed into :class:`AuthService`."""

    _repo: Any
    _otp_service: Any
    _redis: Any
    _email_service: Any
    _settings_repo: Any

    def _settings(self) -> InstanceSettingsRepository:
        """Return the injected settings repo (fallback constructs one)."""
        if self._settings_repo is not None:
            return self._settings_repo
        return InstanceSettingsRepository(self._repo.session)

    async def _registration_mode(self) -> str:
        """Return the platform registration mode (defaults ``invite_only``).

        Single implementation — delegates to
        :meth:`InstanceSettingsRepository.get_registration_mode` so the
        router, setup, signup, and join all read one code path.

        Returns:
            One of ``open``, ``invite_only``, ``approval``.
        """
        return await self._settings().get_registration_mode()

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
        from core.exceptions import AuthorizationError

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
                extra={"email": mask_email(payload.email)},
            )
            return self._signup_success_response(payload.email)

        # No tenant context on this public path — authz is the
        # registration_mode gate above; persistence uses a
        # transaction-local bypass inside create_dashboard_user.  Bind
        # the new user's own context right after creation for the
        # reads below.
        # Open signup mints a member with member defaults (no
        # members:*); promotion is frozen, grant permissions instead.
        try:
            # bcrypt blocks the event loop — offload to a worker thread.
            from services import auth_service as _facade  # via facade (patchable)

            pw_hash = await asyncio.to_thread(
                _facade.hash_password, payload.password
            )
            created = await self._repo.create_dashboard_user(
                email=payload.email,
                password_hash=pw_hash,
                name=payload.email.split("@")[0],
                role="member",
                permissions=list(MEMBER_DEFAULT_PERMISSIONS),
                # Approval gate is atomic: inactive at creation, never a
                # live-then-deactivate window where the user could log in.
                is_active=mode != "approval",
            )
            await set_user_context(self._repo.session, created.id)
        except IntegrityError:
            # Concurrent duplicate signup — the unique email index won.
            # Same generic response as a fresh signup; roll back the aborted
            # transaction so the session stays usable.
            logger.warning(
                "security.signup_existing_email",
                extra={"email": mask_email(payload.email)},
            )
            await self._repo.rollback()
            return self._signup_success_response(payload.email)

        if mode == "approval":
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
