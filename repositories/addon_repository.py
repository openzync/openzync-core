"""Addon repository — all DB access for ``addon_states``."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.addon_state import AddonState


class AddonRepository:
    """Pure query layer for addon install state."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    @property
    def session(self) -> AsyncSession:
        """Expose the session for service-layer transactions."""
        return self._db

    async def get_by_id(self, addon_id: str) -> AddonState | None:
        """Fetch one addon row by id."""
        result = await self._db.execute(
            select(AddonState).where(AddonState.id == addon_id)
        )
        return result.scalar_one_or_none()

    async def get_all(self) -> list[AddonState]:
        """Return every addon row."""
        result = await self._db.execute(select(AddonState))
        return list(result.scalars().all())

    async def create(self, row: AddonState) -> AddonState:
        """Persist a new addon row."""
        self._db.add(row)
        await self._db.flush()
        return row

    async def delete(self, row: AddonState) -> None:
        """Remove an addon row."""
        await self._db.delete(row)
        await self._db.flush()


__all__ = ["AddonRepository"]
