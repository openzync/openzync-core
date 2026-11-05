"""Auth profile — dashboard profile read/update."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from core.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from core.rbac import (
    ALL_PERMISSIONS,
)
from schemas.auth import (
    DashboardUserResponse,
    UpdateProfileRequest,
)

if TYPE_CHECKING:
    from models.user import User

logger = logging.getLogger(__name__)

class ProfileMixin:
    """Composed into :class:`AuthService`."""

    _repo: Any
    _otp_service: Any
    _redis: Any
    _org_repo: Any
    _email_service: Any
    _bao_client: Any

    # ── Profile ──────────────────────────────────────────────────────────────

    @staticmethod
    def _effective_permissions(user: User) -> list[str]:
        """Return the user's effective permission set for the dashboard profile.

        Admin/superadmin roles are wildcards (their stored ``permissions``
        array is empty) — mirror the allow-all branch of
        ``require_permission`` (dependencies/auth.py) by expanding to the
        full vocabulary.  All other roles surface their stored set.

        Args:
            user: The loaded dashboard user.

        Returns:
            Sorted effective permission strings.
        """
        if user.role in ("admin", "superadmin"):
            return sorted(ALL_PERMISSIONS)
        return sorted(user.permissions or [])

    async def get_profile(self, user_id: uuid.UUID) -> DashboardUserResponse:
        """Get the dashboard user's own profile.

        Args:
            user_id: The authenticated user's UUID (from JWT sub claim).

        Returns:
            The user's public profile.

        Raises:
            NotFoundError: If the user no longer exists.
        """
        user = await self._repo.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")
        return DashboardUserResponse(
            id=user.id,
            email=user.email or "",
            name=user.name,
            role=user.role if user.role is not None else "member",
            organization_id=user.organization_id,
            is_email_verified=user.is_email_verified,
            mfa_enabled=user.mfa_enabled,
            must_change_password=bool(user.must_change_password),
            permissions=self._effective_permissions(user),
        )

    async def update_profile(
        self,
        user_id: uuid.UUID,
        payload: UpdateProfileRequest,
    ) -> DashboardUserResponse:
        """Update the dashboard user's profile and/or password.

        Args:
            user_id: The authenticated user's UUID.
            payload: Fields to update. Only non-``None`` fields are applied.

        Returns:
            Updated user profile.

        Raises:
            NotFoundError: If the user no longer exists.
            ValidationError: If password change is requested without
                valid current password, or email change is requested
                without valid current password.
            ConflictError: If the new email is already taken.
        """
        user = await self._repo.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

        has_changes = False
        update_kwargs: dict[str, Any] = {}

        # Profile fields
        if payload.name is not None:
            update_kwargs["name"] = payload.name
            has_changes = True

        if payload.email is not None:
            # Check email uniqueness
            existing = await self._repo.find_user_by_email(payload.email)
            if existing is not None and existing.id != user_id:
                raise ConflictError(
                    f"Email '{payload.email}' is already in use."
                )
            if payload.email.lower() != (user.email or "").lower():
                # Email takeover vector — require password re-authentication,
                # same verify path as the password-change branch below.
                if not payload.current_password:
                    raise ValidationError(
                        "Current password is required to change email."
                    )
                if user.password_hash is None:
                    raise ValidationError(
                        "This account does not have a password set."
                    )
                from services import auth_service as _facade  # via facade (patchable)
                if not _facade.verify_password(
                    payload.current_password, user.password_hash
                ):
                    raise AuthenticationError("Current password is incorrect.")
            update_kwargs["email"] = payload.email
            has_changes = True

            # New email must be re-verified — reset flag and send OTP
            await self._repo.reset_email_verification(user_id)
            await self._otp_service.generate_and_send(
                email=payload.email,
                purpose="signup",
            )

        # Password change
        if payload.new_password is not None:
            if not payload.current_password:
                raise ValidationError(
                    "Current password is required to set a new password."
                )
            if user.password_hash is None:
                raise ValidationError(
                    "This account does not have a password set."
                )
            from services import auth_service as _facade  # via facade
            if not _facade.verify_password(
                payload.current_password, user.password_hash
            ):
                raise AuthenticationError("Current password is incorrect.")
            self._validate_password(payload.new_password)
            update_kwargs["password_hash"] = _facade.hash_password(payload.new_password)
            has_changes = True

            # Send password-change notification email
            if self._email_service is not None:
                user_email = user.email or user.external_id
                if user_email:
                    from services.email_service import (  # noqa: PLC0415
                        render_email_template,
                        render_text_template,
                    )

                    context: dict[str, object] = {
                        "name": user.name or "there",
                    }
                    html_body = await render_email_template("password_changed", context)
                    text_body = await render_text_template("password_changed", context)

                    try:
                        await self._email_service.send_email(
                            to=user_email,
                            subject="Your OpenZync password was changed",
                            html_body=html_body,
                            text_body=text_body,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to send password-change notification email",
                            extra={"email": user_email[:3] + "**@" + user_email.split("@")[-1]},
                        )

        if has_changes:
            user = await self._repo.update_dashboard_user(
                user_id=user_id,
                **update_kwargs,
            )

        return DashboardUserResponse(
            id=user.id,
            email=user.email or "",
            name=user.name,
            role=user.role if user.role is not None else "member",
            organization_id=user.organization_id,
            is_email_verified=user.is_email_verified,
            mfa_enabled=user.mfa_enabled,
            must_change_password=bool(user.must_change_password),
            permissions=self._effective_permissions(user),
        )
