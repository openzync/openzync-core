"""Organization repository — all DB access for organization-specific queries.

Every public method accepts ``organization_id`` to enforce tenant isolation.
No business logic — pure query construction and execution.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class OrganizationRepository:
    """All database access for organizations.

    Every method accepts ``organization_id`` to enforce tenant isolation.
    No business logic — pure query construction and execution.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

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
