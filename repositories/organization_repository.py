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

    # ── Organization rows ───────────────────────────────────────────────────

    async def get_by_id(self, user_id: UUID) -> Organization | None:
        """Fetch an organization by UUID.

        Tenant-scoped read: binds the session to ``user_id`` first so the
        RLS ``FOR ALL`` policy admits the row (mirrors the write-path
        convention in ``create_dashboard_user`` callers).  Public,
        tenant-less callers (invite info/accept) must use
        :meth:`get_platform_org`-style bypasses instead — this method
        intentionally sees only the caller's own org.

        Args:
            user_id: The organization UUID.

        Returns:
            The Organization if found, or ``None``.
        """
        await rls.set_user_context(self._db, user_id)
        result = await self._db.execute(
            select(Organization).where(Organization.id == user_id)
        )
        return result.scalar_one_or_none()

    async def create_organization(self, name: str) -> Organization:
        """Insert an organization row (flush only — the caller commits).

        # shortcut: no src callers (single platform org is ensured via
        # ensure_platform_org); kept for scripts/one-off provisioning,
        # remove when a second org flow exists.

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
            # shortcut: drop the column + backfill once 0044-era deploys are
            # gone; blocked on a NOT NULL migration, not worth it yet.
            org_code=secrets.token_hex(4).upper(),
        )
        await rls.set_user_context(self._db, org.id)
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
                    # shortcut: drop the column when a NOT NULL migration
                    # is scheduled; the random mint is dead weight until then.
                    org_code=secrets.token_hex(4).upper(),
                    is_active=True,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await self._db.flush()

    # ── Config JSONB (Groups A, B, C — UI-exposed settings) ─────────────────

    async def get_config(self, user_id: UUID) -> dict[str, Any]:
        """Read the full ``config`` JSONB column for an organization.

        Tenant-scoped read: binds the session to ``user_id`` first (see
        :meth:`get_by_id`).

        Args:
            user_id: The organization UUID.

        Returns:
            The config dict, or ``{}`` if not configured or the org does
            not exist.
        """
        await rls.set_user_context(self._db, user_id)
        result = await self._db.execute(
            text("SELECT config FROM organizations WHERE id = :user_id"),
            {"user_id": user_id},
        )
        row = result.one_or_none()
        return dict(row.config) if row and row.config else {}

    # ── PII config (from quotas->'pii') ──────────────────────────────────────

    async def get_pii_config(self, user_id: UUID) -> dict:
        """Fetch the PII configuration for an organization.

        The PII config lives at ``organizations.quotas -> 'pii'`` as a JSONB
        sub-document.  Tenant-scoped read (binds ``user_id`` first).

        Args:
            user_id: The organization UUID.

        Returns:
            The PII config dict, or ``{}`` if not configured or the
            organization does not exist.
        """
        await rls.set_user_context(self._db, user_id)
        result = await self._db.execute(
            text(
                "SELECT quotas->'pii' AS pii_config "
                "FROM organizations WHERE id = :user_id"
            ),
            {"user_id": user_id},
        )
        row = result.one_or_none()
        if row is None:
            return {}
        pii_config = row[0]
        return pii_config if isinstance(pii_config, dict) else {}

    async def get_quota(self, user_id: UUID, quota_name: str) -> int | None:
        """Get a specific quota value for an organization.

        Quotas are stored as a JSONB column (``organizations.quotas``)
        keyed by quota name.  Tenant-scoped read (binds ``user_id`` first).

        Args:
            user_id: The organization UUID.
            quota_name: The quota key name (e.g. ``max_users``, ``storage_gb``).

        Returns:
            The quota value as an ``int``, or ``None`` if the quota key is
            not set or the organization does not exist.
        """
        await rls.set_user_context(self._db, user_id)
        result = await self._db.execute(
            text(
                "SELECT quotas->>:quota_name AS quota "
                "FROM organizations WHERE id = :user_id"
            ),
            {"user_id": user_id, "quota_name": quota_name},
        )
        row = result.one_or_none()
        if row and row.quota is not None:
            return int(row.quota)
        return None
