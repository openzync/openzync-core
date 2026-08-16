"""Instance settings repository — DB access for the single-row settings table.

All access is scoped to the fixed row ``id = 1``.  ``get()`` lazily seeds
the row with the ``pg_insert ... ON CONFLICT DO NOTHING`` idiom so
concurrent readers cannot race on the fixed primary key.

No business logic — pure query construction and execution.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.instance_settings import InstanceSettings

_INSTANCE_SETTINGS_PK: int = 1
"""Fixed primary key enforcing single-row semantics."""


class InstanceSettingsRepository:
    """All database access for :class:`InstanceSettings`.

    Args:
        db: An async SQLAlchemy session (request-scoped).
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self) -> InstanceSettings:
        """Return the settings row, seeding it lazily if absent.

        The migration seeds the row; this insert-on-conflict is a
        self-healing fallback for databases migrated from before 0029.

        Returns:
            The single :class:`InstanceSettings` row.
        """
        stmt = (
            pg_insert(InstanceSettings)
            .values(id=_INSTANCE_SETTINGS_PK)
            .on_conflict_do_nothing(index_elements=[InstanceSettings.id])
        )
        await self._db.execute(stmt)
        await self._db.flush()

        result = await self._db.execute(
            select(InstanceSettings).where(InstanceSettings.id == _INSTANCE_SETTINGS_PK)
        )
        row = result.scalar_one_or_none()
        if row is None:
            # Unreachable: the insert above guarantees the row exists.
            row = InstanceSettings(id=_INSTANCE_SETTINGS_PK)
            self._db.add(row)
            await self._db.flush()
        return row

    async def update(self, **fields: Any) -> InstanceSettings:
        """Apply partial field updates to the settings row.

        Args:
            **fields: Column names mapped to new values (e.g.
                ``registration_mode="disabled"``).

        Returns:
            The updated row, refreshed from the database.
        """
        row = await self.get()
        for key, value in fields.items():
            setattr(row, key, value)
        await self._db.flush()
        await self._db.refresh(row)
        return row
