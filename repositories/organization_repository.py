"""Organization repository — all DB access for organization-specific queries.

Every public method accepts ``organization_id`` to enforce tenant isolation.
No business logic — pure query construction and execution.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.organization import Organization


class OrganizationRepository:
    """All database access for organizations.

    Every method accepts ``organization_id`` to enforce tenant isolation.
    No business logic — pure query construction and execution.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    @property
    def session(self) -> AsyncSession:
        """Return the database session for transactional operations.

        Exposed as a public property so service-layer code can run
        multi-table transactions that span multiple repository methods.
        """
        return self._db

    # ── Organization rows ───────────────────────────────────────────────────

    async def get_by_id(self, org_id: UUID) -> Organization | None:
        """Fetch an organization by UUID.

        Args:
            org_id: The organization UUID.

        Returns:
            The Organization if found, or ``None``.
        """
        result = await self._db.execute(
            select(Organization).where(Organization.id == org_id)
        )
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Organization | None:
        """Fetch a joinable organization by its join code.

        The caller normalizes the code (``normalize_org_code``) before
        calling — this method matches exactly.  Inactive, pending,
        rejected, and platform (``SYSTEM``) organizations are excluded:
        a deactivated or unapproved org's code must not accept new
        members.

        Args:
            code: The normalized org code.

        Returns:
            The joinable Organization if found, or ``None``.
        """
        result = await self._db.execute(
            select(Organization).where(
                Organization.org_code == code,
                Organization.is_active.is_(True),
                Organization.status == "approved",
                Organization.name != "SYSTEM",
            )
        )
        return result.scalar_one_or_none()

    async def list_all(
        self,
        status: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[Organization], int]:
        """List all organizations (superadmin view, requires RLS bypass).

        Intended for the platform super-admin org listing — every org
        including pending and rejected, newest first.

        Args:
            status: Optional lifecycle filter — ``pending``, ``approved``,
                or ``rejected``.  ``None`` returns every status.
            page: 1-based page number.
            limit: Page size (clamped to 1..200).

        Returns:
            A tuple of ``(orgs_on_page, total_matching_count)``.
        """
        effective_limit = min(max(limit, 1), 200)
        effective_page = max(page, 1)

        base = select(Organization)
        if status is not None:
            base = base.where(Organization.status == status)

        total = (
            await self._db.execute(select(func.count()).select_from(base.subquery()))
        ).scalar() or 0

        result = await self._db.execute(
            base.order_by(Organization.created_at.desc())
            .offset((effective_page - 1) * effective_limit)
            .limit(effective_limit)
        )
        return result.scalars().all(), total

    async def approve_if_pending(self, org_id: UUID) -> bool:
        """Atomically flip a ``pending`` org to ``approved``.

        Single conditional UPDATE — exactly one concurrent caller wins:
        the loser's statement matches zero rows once the winner's
        transaction commits (READ COMMITTED re-evaluates the WHERE against
        the committed status), so two racing approvals can never both pass
        the ``status == 'pending'`` gate.  Same race-free pattern as
        ``AuthRepository.revoke_refresh_token_if_current``.

        Args:
            org_id: The organization UUID.

        Returns:
            ``True`` if this caller claimed (approved) the org.
        """
        result = await self._db.execute(
            update(Organization)
            .where(
                Organization.id == org_id,
                Organization.status == "pending",
            )
            .values(status="approved")
            .execution_options(synchronize_session=False)
        )
        await self._db.flush()
        return result.rowcount == 1  # type: ignore[attr-defined,no-any-return]

    async def set_status(self, org_id: UUID, status: str) -> Organization:
        """Set an organization's lifecycle status.

        Args:
            org_id: The organization UUID.
            status: One of ``pending``, ``approved``, ``rejected``.

        Returns:
            The updated Organization.

        Raises:
            NotFoundError: If no organization with this UUID exists.
        """
        from core.exceptions import NotFoundError

        result = await self._db.execute(
            select(Organization).where(Organization.id == org_id)
        )
        org = result.scalar_one_or_none()
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        org.status = status
        await self._db.flush()
        await self._db.refresh(org)
        return org

    async def set_org_code(self, org_id: UUID, code: str) -> Organization:
        """Replace an organization's join code (rotation).

        Args:
            org_id: The organization UUID.
            code: The new normalized org code.

        Returns:
            The updated Organization.

        Raises:
            NotFoundError: If no organization with this UUID exists.
        """
        from core.exceptions import NotFoundError

        result = await self._db.execute(
            select(Organization).where(Organization.id == org_id)
        )
        org = result.scalar_one_or_none()
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        org.org_code = code
        await self._db.flush()
        await self._db.refresh(org)
        return org

    async def set_join_enabled(self, org_id: UUID, enabled: bool) -> Organization:
        """Toggle whether the org accepts new members via org-code join.

        Args:
            org_id: The organization UUID.
            enabled: Whether org-code self-registration is accepted.

        Returns:
            The updated Organization.

        Raises:
            NotFoundError: If no organization with this UUID exists.
        """
        from core.exceptions import NotFoundError

        result = await self._db.execute(
            select(Organization).where(Organization.id == org_id)
        )
        org = result.scalar_one_or_none()
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        org.join_enabled = enabled
        await self._db.flush()
        await self._db.refresh(org)
        return org

    # ── Config JSONB (Groups A, B, C — UI-exposed settings) ─────────────────

    async def get_config(self, org_id: UUID) -> dict[str, Any]:
        """Read the full ``config`` JSONB column for an organization.

        Args:
            org_id: The organization UUID.

        Returns:
            The config dict, or ``{}`` if not configured or the org does
            not exist.
        """
        result = await self._db.execute(
            text("SELECT config FROM organizations WHERE id = :org_id"),
            {"org_id": org_id},
        )
        row = result.one_or_none()
        return dict(row.config) if row and row.config else {}

    # ── PII config (from quotas->'pii') ──────────────────────────────────────

    async def get_pii_config(self, org_id: UUID) -> dict:
        """Fetch the PII configuration for an organization.

        The PII config lives at ``organizations.quotas -> 'pii'`` as a JSONB
        sub-document.

        Args:
            org_id: The organization UUID.

        Returns:
            The PII config dict, or ``{}`` if not configured or the
            organization does not exist.
        """
        result = await self._db.execute(
            text(
                "SELECT quotas->'pii' AS pii_config "
                "FROM organizations WHERE id = :org_id"
            ),
            {"org_id": org_id},
        )
        row = result.one_or_none()
        if row is None:
            return {}
        pii_config = row[0]
        return pii_config if isinstance(pii_config, dict) else {}

    # ── Legacy llm_config (deprecated — reads config->'llm' with fallback) ───

    async def get_llm_config(self, org_id: UUID) -> dict[str, Any]:
        """Get the LLM configuration for an organization.

        **DEPRECATED**: Prefer ``get_config()`` which returns the full config
        JSONB.  This method reads from ``config->'llm'`` with a fallback to
        the legacy ``llm_config`` column for backward compatibility during
        the migration window.

        Args:
            org_id: The organization UUID.

        Returns:
            The LLM config dict, or ``{}`` if not configured.
        """
        # Primary: read from new config JSONB
        result = await self._db.execute(
            text(
                "SELECT config->'llm' AS llm FROM organizations WHERE id = :org_id"
            ),
            {"org_id": org_id},
        )
        row = result.one_or_none()
        if row and row.llm is not None and isinstance(row.llm, dict):
            return dict(row.llm)

        # Fallback: legacy llm_config column (data will be migrated by
        # Alembic revision 0002, but keep this for safety)
        result = await self._db.execute(
            text("SELECT llm_config FROM organizations WHERE id = :org_id"),
            {"org_id": org_id},
        )
        row = result.one_or_none()
        return dict(row.llm_config) if row and row.llm_config else {}

    async def get_quota(self, org_id: UUID, quota_name: str) -> int | None:
        """Get a specific quota value for an organization.

        Quotas are stored as a JSONB column (``organizations.quotas``)
        keyed by quota name.

        Args:
            org_id: The organization UUID.
            quota_name: The quota key name (e.g. ``max_users``, ``storage_gb``).

        Returns:
            The quota value as an ``int``, or ``None`` if the quota key is
            not set or the organization does not exist.
        """
        result = await self._db.execute(
            text(
                "SELECT quotas->>:quota_name AS quota "
                "FROM organizations WHERE id = :org_id"
            ),
            {"org_id": org_id, "quota_name": quota_name},
        )
        row = result.one_or_none()
        if row and row.quota is not None:
            return int(row.quota)
        return None
