"""Auth registration — signup plus pending-org creation."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    AuthorizationError,
    ValidationError,
)
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

    # ── Signup ──────────────────────────────────────────────────────────────

    async def signup(
        self, payload: SignupRequest
    ) -> SignupResponse | PendingOrgResponse:
        """Create a new organization with an admin dashboard user.

        Platform policy gate (``get_system_config``) runs FIRST:
        - ``reject_all`` → 403, no org is created.
        - ``approvals`` with ``public_signup`` in scope → the org is
          created as ``pending`` (approval queue) with a pending admin
          user — no OpenBao namespace, no OTP, no live access.
        - ``approvals`` WITHOUT ``public_signup`` in scope → 403.  A
          non-selected channel is rejected, never silently routed to
          instant creation.
        - ``allow_all`` → the current live-org flow below.

        Live flow:
        1. Check email uniqueness (no existing user with this email).
        2. Create the organization.
        3. Hash the password and create the dashboard admin user.
        4. Send an OTP verification code to the user's email.
        5. Return a confirmation message (no tokens — user must verify email).

        The response is identical whether or not the email is already
        registered — the real distinction is logged server-side, so the
        endpoint cannot be used to enumerate existing accounts.

        Args:
            payload: Signup request with email, password, org name.

        Returns:
            A ``SignupResponse`` with a confirmation message, or a
            ``PendingOrgResponse`` when the org enters the approval queue.

        Raises:
            AuthorizationError: If registration is disabled (``reject_all``)
                or the approvals policy excludes the public signup channel.
            ValidationError: If the password does not meet requirements,
                or the org name is reserved (``SYSTEM``).
        """
        # ── Platform policy gate (before any org creation) ────────────────
        from services import auth_service as _facade  # via facade (patchable)
        system_config = await _facade.get_system_config(
            self._redis, self._bao_client
        )
        if system_config.org_creation_policy == "reject_all":
            raise AuthorizationError("Registration is disabled")

        # Reserved name check — the platform org owns "SYSTEM" exclusively.
        self._validate_org_name(payload.organization_name)

        # ── Approval-queue path ────────────────────────────────────────────
        if system_config.org_creation_policy == "approvals":
            # Non-selected channels are rejected — a scope that excludes
            # public signup must not fall through to instant org creation.
            if system_config.approval_scope not in ("public_signup", "both"):
                raise AuthorizationError("Registration is disabled")
            try:
                return await self.create_pending_org_and_admin(
                    organization_name=payload.organization_name,
                    admin_email=str(payload.email),
                    admin_name=payload.email.split("@")[0],
                )
            except IntegrityError:
                # Concurrent duplicate email in the approvals path — the
                # unique email index won.  Same generic success the live
                # path returns (anti-enumeration, smoke contract); roll
                # back the aborted transaction (the pending org row is
                # uncommitted) so the session stays usable.
                logger.warning(
                    "security.signup_existing_email",
                    extra={"email": payload.email},
                )
                await self._repo.rollback()
                return self._signup_success_response(payload.email)

        # ── Live path (allow_all only) ─────────────────────────────────────
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

        # Create organization
        try:
            org = await self._repo.create_organization(
                name=payload.organization_name,
                plan="free",
            )

            # Bind the session to the new tenant.  The repository already
            # bound it (the INSERT's RETURNING clause is filtered by the
            # organizations SELECT policy); this is the flow's explicit
            # guarantee that everything after the org row — the prompt
            # seeding and the admin user insert — is org-scoped.
            await self._org_repo.set_tenant_context(org.id)

            # Seed default prompt templates for the new org
            await self._repo.seed_prompts_for_org(org.id)

            # Create dashboard admin user
            pw_hash = _facade.hash_password(payload.password)
            await self._repo.create_dashboard_user(
                organization_id=org.id,
                email=payload.email,
                password_hash=pw_hash,
                name=payload.email.split("@")[0],  # default name from email
                role="admin",
                permissions=[],  # admin = wildcard via role
            )

            # Send verification OTP — no tokens issued until email is verified.
            await self._otp_service.generate_and_send(
                email=payload.email,
                purpose="signup",
            )
        except IntegrityError:
            # Concurrent duplicate signup — the unique email index won.
            # Same generic response as a fresh signup; roll back the aborted
            # transaction (the session is unusable until then) so no orphan
            # org is committed.
            logger.warning(
                "security.signup_existing_email",
                extra={"email": payload.email},
            )
            await self._repo.rollback()
            return self._signup_success_response(payload.email)

        return self._signup_success_response(payload.email)
    async def create_pending_org_and_admin(
        self,
        *,
        organization_name: str,
        admin_email: str,
        admin_name: str,
    ) -> PendingOrgResponse:
        """Create a pending org + pending admin user (approval queue).

        Shared by the signup approval path and the in-app org-request
        flow.  One transaction: the org row (``status='pending'``) and the
        admin row (``role='admin'``, ``password_hash=None``,
        ``invite_token_hash=None`` — NOT an invite yet).  No OpenBao
        namespace, no OTP — the org is not live until a superadmin
        approves it (``OrganizationService.approve_org`` mints the invite
        then).

        Args:
            organization_name: Desired org name (already validated != SYSTEM).
            admin_email: Email of the designated admin.
            admin_name: Display name for the designated admin.

        Returns:
            A ``PendingOrgResponse`` confirming the request is queued.

        Raises:
            ValidationError: If the org name is reserved (``SYSTEM``).
        """
        self._validate_org_name(organization_name)

        org = await self._repo.create_organization(
            name=organization_name,
            plan="free",
            status="pending",
        )
        # Bind the session to the new tenant — the pending admin insert is
        # org-scoped.
        await self._org_repo.set_tenant_context(org.id)
        # Pending admin — no password, no invite token yet.  The unique
        # email index (ix_user_email_unique) guarantees the admin email
        # cannot belong to any live user; a duplicate raises IntegrityError
        # which the caller maps to 409.
        await self._repo.create_dashboard_user(
            organization_id=org.id,
            email=admin_email,
            password_hash=None,
            name=admin_name,
            role="admin",
            permissions=[],  # admin = wildcard via role
        )
        logger.info(
            "auth.org_pending",
            extra={"org_id": str(org.id), "email": admin_email},
        )
        return PendingOrgResponse(
            message=(
                "Your organization is pending approval. "
                "You will receive an email when it is approved."
            ),
        )
    async def create_live_admin_user(
        self,
        organization_id: uuid.UUID,
        email: str,
        name: str | None,
    ) -> None:
        """Create a live admin user with no password (OTP-activated).

        Used by the in-app org-request ``allow_all`` path: the designated
        admin activates via email OTP (verify-email), then sets a real
        password via the reset flow.

        Args:
            organization_id: The (already live) organization UUID.
            email: The admin's email address.
            name: Optional display name.
        """
        # The requester is authenticated in *their own* org, so the session
        # is bound to that tenant — bind it to the new org instead, or the
        # insert below is rejected by the org-isolation policy.
        await self._org_repo.set_tenant_context(organization_id)
        await self._repo.create_dashboard_user(
            organization_id=organization_id,
            email=email,
            password_hash=None,
            name=name,
            role="admin",
            permissions=[],  # admin = wildcard via role
        )

    @staticmethod
    def _validate_org_name(name: str) -> None:
        """Reject the reserved platform org name ``SYSTEM`` (case-insensitive).

        Args:
            name: The proposed organization name.

        Raises:
            ValidationError: If the name is reserved.
        """
        if name.strip().upper() == "SYSTEM":
            raise ValidationError(
                f"Organization name '{name}' is reserved and cannot be used."
            )
