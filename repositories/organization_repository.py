"""Organization repository — all DB access for organization-specific queries.

Every public method accepts ``organization_id`` to enforce tenant isolation.
No business logic — pure query construction and execution.
"""

from __future__ import annotations

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
