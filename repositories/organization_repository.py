"""Organization repository — all DB access for the singleton platform org.

Single platform org (``PLATFORM_ORG_ID``): this repository exposes only
platform-row access plus the per-org ``config``/``quotas`` JSONB reads.
No business logic — pure query construction and execution.
"""

from __future__ import annotations

import secrets
import uuid as uuid_lib
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core import rls
from core.config import PLATFORM_ORG_ID
from models.organization import Organization


class OrganizationRepository:
    """All database access for the singleton platform organization."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    @property
    def session(self) -> AsyncSession:
        """Return the database session for transactional operations.

        Exposed as a public property so service-layer code can run
        multi-table transactions that span multiple repository methods.
        """
        return self._db

    # ── Tenant context ──────────────────────────────────────────────────────

    async def set_tenant_context(self, org_id: UUID) -> None:
        """Bind this session to one tenant (organization).

        The public flows (signup, join, login, refresh) only learn their
        tenant *after* their first lookup, so they bind the session to the
        resolved organization before any org-scoped write.  Session-scoped,
        so the tenant survives a mid-request commit.

        Args:
            org_id: The organization UUID the session may act on.
        """
        await rls.set_tenant_context(self._db, org_id)

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

    async def create_organization(self, name: str) -> Organization:
        """Insert an organization row (flush only — the caller commits).

        The id is minted here so the session can be bound to the new
        tenant before the row is written (see ``set_tenant_context``).

        Args:
            name: Human-readable organization name.

        Returns:
            The newly created Organization (flushed, uncommitted).
        """
        org = Organization(
            id=uuid_lib.uuid4(),
            name=name,
            # Legacy NOT NULL column — join codes are removed (single org);
            # a random placeholder keeps the constraint satisfied.
            org_code=secrets.token_hex(4).upper(),
        )
        await self.set_tenant_context(org.id)
        self._db.add(org)
        await self._db.flush()
        await self._db.refresh(org)
        return org

    async def get_platform_org(self) -> Organization | None:
        """Fetch the platform (SYSTEM) organization, bypassing RLS.

        The setup probe and the one-time setup flow run without a tenant
        context, so the lookup disables the RLS policies transaction-
        locally instead of relying on ``set_config`` at the call site.

        Returns:
            The platform Organization if seeded, or ``None``.
        """
        async with rls.rls_bypass(self._db):
            result = await self._db.execute(
                select(Organization).where(Organization.id == PLATFORM_ORG_ID)
            )
            return result.scalar_one_or_none()

    async def ensure_platform_org(self, name: str = "Platform") -> None:
        """Insert the platform organization if it does not exist.

        Idempotent ``ON CONFLICT DO NOTHING`` — concurrent setup calls
        race safely, the losers no-op.  Runs under an RLS bypass so it
        works without a tenant context.  When the row already exists its
        name is left untouched (bootstrap naming applies once).

        Args:
            name: Display name for the singleton org (bootstrap only).
        """
        async with rls.rls_bypass(self._db):
            await self._db.execute(
                pg_insert(Organization)
                .values(
                    id=PLATFORM_ORG_ID,
                    name=name,
                    status="approved",
                    join_enabled=False,
                    # Legacy NOT NULL column — join codes are removed;
                    # a random placeholder keeps the constraint satisfied.
                    org_code=secrets.token_hex(4).upper(),
                    is_active=True,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await self._db.flush()

    async def set_join_enabled(self, org_id: UUID, enabled: bool) -> Organization:
        """Toggle whether the platform org accepts self-registration.

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
