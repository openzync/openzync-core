"""Invite service — the admin invite-by-email flow.

An admin invites a user by email; the invitee receives a magic link and
sets a password to activate the account.  This service owns the whole
flow: create the pending row, email the magic link, revoke, and accept.

Key invariants (mirror the repo's zero-fallback discipline):

- **Create row first, then send email.**  If sending fails the
  ``ExternalServiceError`` propagates and the request-scoped session
  dependency rolls the transaction back — nothing orphaned, no
  commit-then-send.
- **Accept is atomic.**  ``UserRepository.claim_invite`` is a single
  conditional UPDATE ... RETURNING; exactly one concurrent caller wins.
  The returned ``id``/``organization_id``/``role`` feed straight into
  ``AuthService.issue_tokens`` — never a select-then-update.
- **Expired and used are indistinguishable.**  Both accept and info return
  the same generic 404 ("This invitation link is invalid or has expired.").
  An expired-but-present row is self-cleaned (hard-deleted) so stale rows
  cannot accumulate.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid  # noqa: TC003
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from core.config import get_settings
from core.exceptions import ConflictError, ExternalServiceError, NotFoundError
from repositories.organization_repository import (  # noqa: TC001
    OrganizationRepository,
)
from repositories.user_repository import UserRepository  # noqa: TC001
from schemas.auth import (
    InviteInfoResponse,
    InviteRequest,
    InviteResponse,
    TokenResponse,
)
from utils.password import hash_password

if TYPE_CHECKING:
    from services.auth_service import AuthService
    from services.email_service import EmailService

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

INVITE_EXPIRY_HOURS = 72
"""Lifetime of a pending invite, in hours.

Single source of the expiry rule: ``accept_invite`` derives the claim
cutoff from this constant and passes it to
``UserRepository.claim_invite`` as a bound parameter, and the info-path
self-clean check uses it directly.  The SQL-side claim guard can no
longer drift from the service-side check.
"""

_GENERIC_INVITE_404 = "This invitation link is invalid or has expired."
"""Single 404 message for used, expired, and unknown invite tokens.

Deliberately generic — expired vs used vs never-existed must be
indistinguishable to the caller (anti-enumeration by design).
"""


def _hash_invite_token(raw: str) -> str:
    """Deterministic SHA-256 hash of a raw invite token for DB storage.

    Args:
        raw: The opaque magic-link token string.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    return hashlib.sha256(raw.encode()).hexdigest()


async def send_invite_email(
    email_service: EmailService,
    *,
    org_name: str,
    inviter_name: str,
    invitee_name: str,
    invitee_email: str,
    raw_token: str,
    locale: str = "en",
) -> None:
    """Render and send the magic-link invite email.

    Shared by the admin invite flow and the platform org-approval flow
    (``OrganizationService.approve_org``) — one template, one link shape
    (``{FRONTEND_URL}/invite?token=...``).

    Args:
        email_service: The email service used to send.
        org_name: Inviting organization's name (subject + body).
        inviter_name: Name of the inviting actor (admin or superadmin).
        invitee_name: Invitee display name (greeting).
        invitee_email: Recipient address.
        raw_token: The plaintext magic-link token — appears only in the
            emailed link, never in logs or responses.
        locale: Recipient's BCP-47 locale tag — selects the template
            language (falls back to English).

    Raises:
        ExternalServiceError: If the email service is unavailable or
            sending fails — propagates so the pending row is rolled back.
    """
    from services.email_service import (  # noqa: PLC0415
        render_email_template,
        render_subject_template,
        render_text_template,
    )

    link = f"{get_settings().FRONTEND_URL}/invite?token={raw_token}"
    context: dict[str, object] = {
        "org_name": org_name,
        "inviter_name": inviter_name,
        "invitee_name": invitee_name,
        "invitee_email": invitee_email,
        "link": link,
        "expiry_hours": INVITE_EXPIRY_HOURS,
    }
    html_body = await render_email_template("invite", context, locale=locale)
    text_body = await render_text_template("invite", context, locale=locale)
    subject = await render_subject_template("invite", context, locale=locale)

    await email_service.send_email(
        to=invitee_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════════════════════════════════


class InviteService:
    """Admin invite-by-email orchestration.

    Args:
        repo: User repository — pending-row CRUD and the atomic claim.
        auth_service: Auth service — token issuance and the shared password
            strength rule.
        email_service: Email service for the magic-link delivery.  ``None``
            is a loud failure at invite time (the email *is* the invite) —
            there is no silent no-email path.
        org_repo: Organization repository — org-name lookups for the email
            and for ``InviteInfoResponse``.
    """

    def __init__(
        self,
        repo: UserRepository,
        auth_service: AuthService,  # noqa: F821
        email_service: EmailService | None,  # noqa: F821
        org_repo: OrganizationRepository,
    ) -> None:
        self._repo = repo
        self._auth_service = auth_service
        self._email_service = email_service
        self._org_repo = org_repo

    # ── Invite ─────────────────────────────────────────────────────────────

    async def invite_user(
        self,
        admin_user_id: uuid.UUID,
        org_id: uuid.UUID,
        payload: InviteRequest,
    ) -> InviteResponse:
        """Create a pending member user and email them the magic link.

        Flow:
        1. Reject duplicate emails loudly (admin-facing — no anti-enumeration
           here; the admin already knows who they invited).
        2. Resolve the org (email body) and the admin (inviter name) —
           a missing org or admin is a loud 404, before any row is created.
        3. Create the user row with ``password_hash=NULL`` and the SHA-256
           hash of a fresh random token.
        4. Render + send the invite email.  A send failure raises
           ``ExternalServiceError``; the request-scoped session rolls the
           row back — nothing orphaned.

        Args:
            admin_user_id: UUID of the inviting admin (for the email's
                "invited you" attribution).
            org_id: The inviting organization.
            payload: Invitee email + name.

        Returns:
            The pending user's id/email/name — never the raw token.

        Raises:
            ConflictError: If an account with this email already exists.
            NotFoundError: If the org or the admin user does not exist.
            ExternalServiceError: If the invite email cannot be sent (the
                row is rolled back with it).
        """
        existing = await self._repo.find_user_by_email(payload.email)
        if existing is not None:
            logger.warning(
                "invite.duplicate_email",
                extra={"email": payload.email, "org_id": str(org_id)},
            )
            raise ConflictError("An account with this email already exists.")

        # ⚠️ RACE: the email uniqueness check is application-level only — the
        # `ix_user_email_unique` index is non-unique, so two concurrent
        # invites for the same email can both pass and create two pending
        # rows (same as signup/join — pre-existing gap).  A unique partial
        # index on (email) would close it; out of scope here.
        org = await self._org_repo.get_by_id(org_id)
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        admin = await self._repo.get_by_uuid(org_id, admin_user_id)
        if admin is None:
            raise NotFoundError(f"Admin user {admin_user_id} not found.")

        raw_token = secrets.token_urlsafe(32)
        token_hash = _hash_invite_token(raw_token)

        # Create the row FIRST — the token credential and the pending user
        # are born atomically; the email send happens after.
        user = await self._repo.create(
            organization_id=org_id,
            external_id=payload.email,
            name=payload.name,
            email=payload.email,
            role="member",
            password_hash=None,
            invite_token_hash=token_hash,
        )
        logger.info(
            "invite.created",
            extra={
                "user_id": str(user.id),
                "org_id": str(org_id),
                "email": payload.email,
            },
        )

        await self._send_invite_email(
            org_name=org.name,
            inviter_name=admin.name or "An admin",
            invitee_name=payload.name,
            invitee_email=payload.email,
            raw_token=raw_token,
            locale=user.locale,
        )

        return InviteResponse(
            id=user.id,
            email=payload.email,
            name=payload.name,
        )

    # ── Revoke ─────────────────────────────────────────────────────────────

    async def revoke_invite(self, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Revoke a pending invite by hard-deleting its user row.

        Only rows that still carry a non-NULL ``invite_token_hash`` in this
        org are affected — an accepted or ordinary user is never touched.

        Args:
            org_id: The admin's organization (tenant scope).
            user_id: The pending user's UUID.

        Raises:
            NotFoundError: If there is no pending invite for this user in
                this org.
        """
        deleted = await self._repo.hard_delete_pending_user(
            organization_id=org_id,
            user_id=user_id,
        )
        if deleted == 0:
            logger.warning(
                "invite.revoke_miss",
                extra={"user_id": str(user_id), "org_id": str(org_id)},
            )
            raise NotFoundError("No pending invite for this user.")

    # ── Info ───────────────────────────────────────────────────────────────

    async def get_invite_info(self, token: str) -> InviteInfoResponse:
        """Resolve the org/email/name shown on the invite landing page.

        A valid unexpired token returns the invite details; anything else
        (unknown, expired, already used) is the generic 404.  An expired
        but still-present row is hard-deleted here — self-clean, so the
        accept path never sees it.

        Args:
            token: The raw magic-link token.

        Returns:
            The invitee's org name, email, and name.

        Raises:
            NotFoundError: Generic message for unknown, expired, or used
                tokens.
        """
        token_hash = _hash_invite_token(token)
        user = await self._repo.find_user_by_invite_token(token_hash)
        if user is None:
            raise NotFoundError(_GENERIC_INVITE_404)

        if self._is_expired(user.created_at):
            await self._repo.hard_delete_pending_user(
                organization_id=user.organization_id,
                user_id=user.id,
            )
            # Persist the self-clean NOW — the raise immediately after would
            # otherwise be rolled back by the request-scoped session
            # dependency, leaving the garbage row behind (indistinguishable
            # from a hash-miss 404).
            await self._repo.commit()
            raise NotFoundError(_GENERIC_INVITE_404)

        org = await self._org_repo.get_by_id(user.organization_id)
        if org is None:
            # Org deleted since the invite — the invite is dead.
            raise NotFoundError(_GENERIC_INVITE_404)

        return InviteInfoResponse(
            org_name=org.name,
            email=user.email or "",
            name=user.name or "",
        )

    # ── Accept ─────────────────────────────────────────────────────────────

    async def accept_invite(
        self,
        token: str,
        password: str,
    ) -> TokenResponse:
        """Claim a pending invite and log the invitee in.

        Atomic claim first (single conditional UPDATE ... RETURNING), then
        issue tokens from the returned identity — never select-then-update.
        A miss is either a used/unknown token or an expired one; the
        expired-but-present case is self-cleaned and both raise the generic
        404 (indistinguishable by design).

        Args:
            token: The raw magic-link token.
            password: The invitee's chosen password (strength-checked with
                the shared auth rule).

        Returns:
            A fresh JWT pair — the invitee is authenticated immediately.

        Raises:
            ValidationError: If the password is too weak.
            NotFoundError: Generic message for unknown, expired, or used
                tokens.
        """
        self._validate_password(password)
        token_hash = _hash_invite_token(token)
        pw_hash = hash_password(password)

        # Naive UTC (auth_service storage convention) — the single source
        # of the expiry window is INVITE_EXPIRY_HOURS above.
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            hours=INVITE_EXPIRY_HOURS
        )
        claimed = await self._repo.claim_invite(
            token_hash=token_hash,
            password_hash=pw_hash,
            cutoff=cutoff,
        )
        if claimed is None:
            # Miss — distinguish "expired but present" (self-clean) from
            # "used or never existed".  Both end in the same generic 404.
            stale = await self._repo.find_user_by_invite_token(token_hash)
            if stale is not None and self._is_expired(stale.created_at):
                await self._repo.hard_delete_pending_user(
                    organization_id=stale.organization_id,
                    user_id=stale.id,
                )
                # Persist the self-clean NOW — same rationale as the info
                # path: the raise below rolls the delete back otherwise.
                await self._repo.commit()
            raise NotFoundError(_GENERIC_INVITE_404)

        logger.info(
            "invite.accepted",
            extra={"user_id": str(claimed.id), "org_id": str(claimed.organization_id)},
        )
        return await self._auth_service.issue_tokens(
            user_id=claimed.id,
            organization_id=claimed.organization_id,
            role=claimed.role,
        )

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _send_invite_email(
        self,
        *,
        org_name: str,
        inviter_name: str,
        invitee_name: str,
        invitee_email: str,
        raw_token: str,
        locale: str,
    ) -> None:
        """Render and send the magic-link invite email.

        Args:
            org_name: Inviting organization's name (subject + body).
            inviter_name: Name of the admin who sent the invite.
            invitee_name: Invitee display name (greeting).
            invitee_email: Recipient address.
            raw_token: The plaintext magic-link token — appears only in the
                emailed link, never in logs or responses.
            locale: Invitee's BCP-47 locale tag (template language).

        Raises:
            ExternalServiceError: If the email service is unavailable or
                sending fails — propagates so the pending row is rolled back.
        """
        if self._email_service is None:
            raise ExternalServiceError(
                "Email service is not configured — cannot send invite."
            )

        await send_invite_email(
            self._email_service,
            org_name=org_name,
            inviter_name=inviter_name,
            invitee_name=invitee_name,
            invitee_email=invitee_email,
            raw_token=raw_token,
            locale=locale,
        )

    def _is_expired(self, created_at: datetime) -> bool:
        """True if a pending invite is older than the 72-hour window.

        Args:
            created_at: The user row's creation timestamp (timezone-aware).

        Returns:
            ``True`` when the invite can no longer be claimed.
        """
        return created_at < datetime.now(UTC) - timedelta(hours=INVITE_EXPIRY_HOURS)

    @staticmethod
    def _validate_password(password: str) -> None:
        """Validate a password against the shared auth strength rule.

        Reuses ``AuthService._validate_password`` — one rule for every
        place a dashboard password is set (signup, reset, accept).

        Args:
            password: The plaintext password.

        Raises:
            ValidationError: If the password is too weak.
        """
        from services.auth_service import AuthService  # noqa: PLC0415

        AuthService._validate_password(password)
