"""Organization service — bootstrap and management business logic.

This is intentionally kept separate from the main domain services because
the bootstrap flow (creating the first organization) has no authentication
requirement and runs before any user exists.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ConflictError, NotFoundError
from core.openbao import OpenBaoClient
from core.org_codes import generate_org_code
from models.organization import Organization
from models.user import User
from repositories.organization_repository import OrganizationRepository
from repositories.user_repository import UserRepository
from schemas.organizations import CreateOrgRequest, CreateOrgResponse

if TYPE_CHECKING:
    from services.email_service import EmailService
    from services.invite_service import send_invite_email  # noqa: F401  — re-export

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class OrgCodeInfo:
    """Org-code admin view — the join code plus its registration toggle."""

    org_code: str
    join_enabled: bool


class OrganizationService:
    """Business logic for organization bootstrap and management.

    Args:
        repo: Repository for organization DB access.
        bao_client: Optional OpenBao client for per-org namespace + config
            seeding at org creation time.
        email_service: Optional email service — required for the approval
            flow (the pending admin's magic-link invite).  ``None`` makes
            ``approve_org`` fail loudly rather than silently skip the
            email (the email *is* the approval).
    """

    def __init__(
        self,
        repo: OrganizationRepository,
        bao_client: OpenBaoClient | None = None,
        email_service: EmailService | None = None,  # noqa: F821
    ) -> None:
        self._repo = repo
        # Session for multi-table transactions in create_organization.
        # Exposed via OrganizationRepository.session as a public property.
        self._db: AsyncSession = repo.session
        self._bao_client = bao_client
        self._email_service = email_service

    async def create_organization(self, payload: CreateOrgRequest) -> CreateOrgResponse:
        """Create a new organization.

        Performs a single atomic transaction:
        1. Creates an ``Organization`` record.
        2. Seeds default prompt templates.
        3. Bootstraps the OpenBao namespace + default config after commit
           (non-fatal on failure — reconcilable later).

        No default project and no API key are created; first-use
        authentication flows through ``POST /v1/auth/signup``.

        Args:
            payload: Organization name and optional plan.

        Returns:
            A ``CreateOrgResponse`` with the org ID and name.

        Raises:
            ValidationError: If the org name is reserved (``SYSTEM``).
        """
        self._validate_org_name(payload.name)

        # ── 1. Create organization ───────────────────────────────────────
        org = Organization(
            name=payload.name,
            plan=payload.plan,
            org_code=generate_org_code(),
        )
        self._db.add(org)
        await self._db.flush()
        await self._db.refresh(org)

        # ── 2. Seed default prompt templates for the new org ─────────────
        from repositories.prompt_template_repository import PromptTemplateRepository

        seeded = await PromptTemplateRepository(self._db).seed_default_prompts(org.id)
        if seeded:
            logger.info(
                "organization.prompts_seeded",
                org_id=str(org.id),
                count=seeded,
            )

        # ── 3. Commit everything atomically ──────────────────────────────
        await self._db.commit()

        # ── 4. Bootstrap OpenBao namespace + default config ──────────────
        if self._bao_client is not None:
            try:
                await self._bao_client.create_org_namespace(org.id)
                defaults = self._load_org_defaults()
                if defaults:
                    await self._bao_client.write_org_config(org.id, defaults)
                logger.info(
                    "organization.openbao_bootstrapped",
                    org_id=str(org.id),
                    defaults_count=len(defaults),
                )
            except Exception:
                # ⚠️ Non-fatal: if OpenBao is down during org creation we
                #    still return success — the namespace can be bootstrapped
                #    later by an admin or a background reconciliation job.
                logger.exception(
                    "organization.openbao_bootstrap_failed",
                    org_id=str(org.id),
                )

        logger.info(
            "organization.created",
            org_id=str(org.id),
            org_name=org.name,
            org_plan=payload.plan,
        )

        return CreateOrgResponse(
            organization_id=org.id,
            organization_name=org.name,
        )

    # ── Approval flow (platform superadmin) ──────────────────────────────────

    async def approve_org(self, org_id: UUID, actor_id: UUID) -> Organization:
        """Approve a pending org: make it live and invite its admin.

        Flow:
        1. The org must be ``status='pending'`` (else 409).
        2. Flip the status to ``approved``.
        3. Bootstrap the org's OpenBao namespace (+ default config) —
           OpenBao errors propagate, rolling the status change back.
        4. Mint an invite-style magic link for the pending admin user and
           email it — the admin sets their password via the existing
           accept-invite path.

        The caller must use the RLS-bypass superadmin session
        (``get_db_superadmin``) — the pending org is not visible to any
        tenant session.

        Args:
            org_id: The pending organization UUID.
            actor_id: The approving superadmin's user UUID (invite
                attribution).

        Returns:
            The approved Organization.

        Raises:
            NotFoundError: If the org does not exist.
            ConflictError: If the org is not pending, or no pending admin
                user exists for it.
            ExternalServiceError: If the invite email cannot be sent (the
                approval is rolled back with it).
        """
        org = await self._repo.get_by_id(org_id)
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        if org.status != "pending":
            raise ConflictError(
                f"Organization {org_id} is not pending (status={org.status!r})."
            )

        # Atomic claim — the status flip is a single conditional UPDATE
        # (WHERE id AND status='pending'), so exactly one of two concurrent
        # approvals wins.  A loser's UPDATE matches zero rows and raises
        # 409 here, BEFORE any side effect: no second invite is minted and
        # no duplicate approval email is sent.
        if not await self._repo.approve_if_pending(org_id):
            org = await self._repo.get_by_id(org_id)
            if org is None:
                raise NotFoundError(f"Organization {org_id} not found.")
            raise ConflictError(
                f"Organization {org_id} is not pending (status={org.status!r})."
            )

        # Re-read the (now approved) row — the atomic UPDATE owns it.  The
        # attribute is set on the instance so the returned object (and the
        # router's {id, name, status} response) is coherent.
        org = await self._repo.get_by_id(org_id)
        if org is None:  # unreachable — the UPDATE only matched an existing row
            raise NotFoundError(f"Organization {org_id} not found.")
        org.status = "approved"

        # Pending admin — the designated admin of the approval request.
        user_repo = UserRepository(self._db)
        pending_admin = await user_repo.find_pending_admin_by_org(org_id)
        if pending_admin is None:
            raise ConflictError(
                f"Organization {org_id} has no pending admin user to approve."
            )
        admin_email = pending_admin.email
        if admin_email is None:
            raise ConflictError(
                f"Organization {org_id}'s pending admin has no email."
            )

        # OpenBao bootstrap — errors propagate and roll the approval back.
        if self._bao_client is not None:
            await self._bao_client.create_org_namespace(org_id)
            defaults = self._load_org_defaults()
            if defaults:
                await self._bao_client.write_org_config(org_id, defaults)

        # Mint the invite — the pending admin claims it via accept-invite.
        from services.invite_service import _hash_invite_token, send_invite_email

        raw_token = secrets.token_urlsafe(32)
        await user_repo.set_invite_token(
            organization_id=org_id,
            user_id=pending_admin.id,
            token_hash=_hash_invite_token(raw_token),
        )

        # Inviter attribution: the actor is a platform superadmin — their
        # identity lives in the platform org, not in this pending org, so a
        # static label is used (the actor_id is still on the audit trail).
        inviter_name = "A platform administrator"
        if self._email_service is None:
            raise ConflictError(
                "Email service is not configured — cannot send the approval invite."
            )
        await send_invite_email(
            self._email_service,
            org_name=org.name,
            inviter_name=inviter_name,
            invitee_name=pending_admin.name or admin_email.split("@")[0],
            invitee_email=admin_email,
            raw_token=raw_token,
        )

        logger.info(
            "organization.approved",
            org_id=str(org_id),
            actor_id=str(actor_id),
            admin_email=admin_email,
        )
        return org

    async def reject_org(self, org_id: UUID, actor_id: UUID) -> Organization:
        """Reject a pending org (no email is sent).

        The pending admin is simply never invited; the org stays in the
        listing as ``rejected`` for the superadmin's audit trail.  The
        caller must use the RLS-bypass superadmin session.

        Args:
            org_id: The pending organization UUID.
            actor_id: The rejecting superadmin's user UUID (logged).

        Returns:
            The rejected Organization.

        Raises:
            NotFoundError: If the org does not exist.
            ConflictError: If the org is not pending.
        """
        org = await self._repo.get_by_id(org_id)
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        if org.status != "pending":
            raise ConflictError(
                f"Organization {org_id} is not pending (status={org.status!r})."
            )
        org.status = "rejected"
        await self._db.flush()
        await self._db.refresh(org)
        logger.info(
            "organization.rejected",
            org_id=str(org_id),
            actor_id=str(actor_id),
        )
        return org

    # ── Platform superadmin cross-org surface (routers/admin_system) ────────

    async def list_all_orgs(
        self,
        status: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[Organization], int]:
        """List every organization (pending/approved/rejected) for a superadmin.

        Args:
            status: Optional lifecycle filter (pending/approved/rejected).
            page: 1-based page number.
            limit: Page size (clamped to 1..200 by the repository).

        Returns:
            A tuple of ``(orgs_on_page, total_matching_count)``.
        """
        return await self._repo.list_all(status=status, page=page, limit=limit)

    async def list_org_members(
        self,
        org_id: UUID,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[User], int]:
        """List a target org's dashboard users (superadmin cross-org view).

        Args:
            org_id: The target organization UUID.
            page: 1-based page number.
            limit: Page size (clamped to 1..200 by the repository).

        Returns:
            A tuple of ``(users_on_page, total_matching_count)``.

        Raises:
            NotFoundError: If no organization with this UUID exists.
        """
        org = await self._repo.get_by_id(org_id)
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        return await UserRepository(self._db).list_by_org(
            org_id, page=page, limit=limit
        )

    @staticmethod
    def _validate_org_name(name: str) -> None:
        """Reject the reserved platform org name ``SYSTEM`` (case-insensitive).

        Args:
            name: The proposed organization name.

        Raises:
            ValidationError: If the name is reserved.
        """
        from core.exceptions import ValidationError

        if name.strip().upper() == "SYSTEM":
            raise ValidationError(
                f"Organization name '{name}' is reserved and cannot be used."
            )

    # ── Org join code (admin management) ──────────────────────────────────────

    async def get_org_code(self, org_id: UUID) -> OrgCodeInfo:
        """Return the organization's current join code and registration state.

        Args:
            org_id: The organization UUID.

        Returns:
            An ``OrgCodeInfo`` with the org code and ``join_enabled`` flag.

        Raises:
            NotFoundError: If no organization with this UUID exists.
        """
        org = await self._repo.get_by_id(org_id)
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        return OrgCodeInfo(org_code=org.org_code, join_enabled=org.join_enabled)

    async def set_join_enabled(self, org_id: UUID, enabled: bool) -> OrgCodeInfo:
        """Toggle whether the org accepts new members via org-code join.

        Args:
            org_id: The organization UUID.
            enabled: Whether org-code self-registration is accepted.

        Returns:
            The fresh ``OrgCodeInfo`` (code unchanged, toggle updated).

        Raises:
            NotFoundError: If no organization with this UUID exists.
        """
        org = await self._repo.set_join_enabled(org_id, enabled)
        return OrgCodeInfo(org_code=org.org_code, join_enabled=org.join_enabled)

    async def regenerate_org_code(self, org_id: UUID) -> OrgCodeInfo:
        """Generate and persist a new join code (rotating the old one).

        Immediately invalidates any previously distributed code.

        Args:
            org_id: The organization UUID.

        Returns:
            The fresh ``OrgCodeInfo`` with the new org code.

        Raises:
            NotFoundError: If no organization with this UUID exists.
        """
        new_code = generate_org_code()
        org = await self._repo.set_org_code(org_id, new_code)
        return OrgCodeInfo(org_code=org.org_code, join_enabled=org.join_enabled)

    def _load_org_defaults(self) -> dict[str, Any]:
        """Load default per-org config values from ``config/defaults/org_config.yaml``.

        Returns:
            A flat dict of key/value pairs, or ``{}`` if the file is missing
            or unreadable.
        """
        path = Path(__file__).parent.parent / "config" / "defaults" / "org_config.yaml"
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("org_config.defaults_file_not_found", path=str(path))
            return {}
        except yaml.YAMLError as e:
            logger.warning("org_config.defaults_file_invalid", path=str(path), error=str(e))
            return {}
