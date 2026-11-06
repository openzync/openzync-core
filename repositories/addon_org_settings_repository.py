"""Per-org addon settings repository — all DB access for org addon flags."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.addon_org_settings import AddonOrgSettings


class AddonOrgSettingsRepository:
    """Persist per-org addon enable flags and plain settings values."""

    def __init__(self, db: AsyncSession) -> None:
        """Store the request-scoped session.

        Args:
            db: The org-scoped (RLS) session — callers see only their rows.
        """
        self._db = db

    async def get(self, addon_id: str, org_id: uuid.UUID) -> AddonOrgSettings | None:
        """Return one org's row for one addon, or ``None`` when never set.

        Args:
            addon_id: The addon slug.
            org_id: The organization UUID.

        Returns:
            The row, or ``None`` (equivalent to disabled with no values).
        """
        result = await self._db.execute(
            select(AddonOrgSettings).where(
                AddonOrgSettings.addon_id == addon_id,
                AddonOrgSettings.organization_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_all_for_org(self, org_id: uuid.UUID) -> list[AddonOrgSettings]:
        """Return every row this org has ever set.

        Args:
            org_id: The organization UUID.

        Returns:
            The org's rows, ordered by addon id.
        """
        result = await self._db.execute(
            select(AddonOrgSettings)
            .where(AddonOrgSettings.organization_id == org_id)
            .order_by(AddonOrgSettings.addon_id)
        )
        return list(result.scalars().all())

    async def is_enabled(self, addon_id: str, org_id: uuid.UUID) -> bool:
        """Whether the org enabled the addon (no row means disabled).

        Args:
            addon_id: The addon slug.
            org_id: The organization UUID.

        Returns:
            ``True`` only when a row exists and its flag is set.
        """
        row = await self.get(addon_id, org_id)
        return row is not None and row.enabled

    async def set_enabled(
        self, addon_id: str, org_id: uuid.UUID, enabled: bool
    ) -> AddonOrgSettings:
        """Flip one org's flag, creating the row when absent.

        Args:
            addon_id: The addon slug.
            org_id: The organization UUID.
            enabled: The new per-org flag value.

        Returns:
            The persisted row.
        """
        return await self.upsert(addon_id, org_id, enabled=enabled)

    async def upsert(
        self,
        addon_id: str,
        org_id: uuid.UUID,
        values: dict | None = None,
        enabled: bool | None = None,
    ) -> AddonOrgSettings:
        """Create or update one org's row (``None`` fields keep their value).

        Args:
            addon_id: The addon slug.
            org_id: The organization UUID.
            values: Replacement plain values, or ``None`` to keep.
            enabled: New flag, or ``None`` to keep.

        Returns:
            The persisted row.
        """
        row = await self.get(addon_id, org_id)
        if row is None:
            row = AddonOrgSettings(
                addon_id=addon_id, organization_id=org_id, values=values or {}
            )
            if enabled is not None:
                row.enabled = enabled
            self._db.add(row)
        else:
            if values is not None:
                row.values = values
            if enabled is not None:
                row.enabled = enabled
        await self._db.flush()
        await self._db.refresh(row)
        return row
