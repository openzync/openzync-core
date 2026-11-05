"""Addon state repository — all DB access for addon lifecycle rows."""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.addons.manifest import TABLE_NAME_RE
from core.exceptions import ValidationError
from models.addon_state import AddonState


class AddonRepository:
    """Persist addon install/enable state (platform-scoped, no RLS)."""

    def __init__(self, db: AsyncSession) -> None:
        """Store the request-scoped session.

        Args:
            db: The superadmin (bypass) session — addon state is global.
        """
        self._db = db

    async def get_by_id(self, addon_id: str) -> AddonState | None:
        """Return one addon row, or ``None`` when not installed.

        Args:
            addon_id: The addon slug.

        Returns:
            The row, or ``None``.
        """
        result = await self._db.execute(
            select(AddonState).where(AddonState.id == addon_id)
        )
        return result.scalar_one_or_none()

    async def get_all(self) -> list[AddonState]:
        """Return every installed addon row ordered by id.

        Returns:
            All rows, install order irrelevant.
        """
        result = await self._db.execute(select(AddonState).order_by(AddonState.id))
        return list(result.scalars().all())

    async def exists(self, addon_id: str) -> bool:
        """Whether the addon row exists.

        Args:
            addon_id: The addon slug.

        Returns:
            ``True`` when installed.
        """
        return await self.get_by_id(addon_id) is not None

    async def create(self, addon_id: str, version: str, manifest: dict) -> AddonState:
        """Insert the install row (enabled).

        Args:
            addon_id: The addon slug.
            version: The installed version.
            manifest: The validated manifest snapshot.

        Returns:
            The persisted row.
        """
        row = AddonState(
            id=addon_id,
            version=version,
            enabled=True,
            status="installed",
            manifest=manifest,
        )
        self._db.add(row)
        await self._db.flush()
        await self._db.refresh(row)
        return row

    async def update(
        self,
        addon_id: str,
        version: str | None = None,
        enabled: bool | None = None,
        status: str | None = None,
    ) -> AddonState:
        """Update version/enabled/status in place.

        Args:
            addon_id: The addon slug.
            version: New version (upgrade), or ``None`` to keep.
            enabled: New flag, or ``None`` to keep.
            status: New lifecycle marker, or ``None`` to keep.

        Returns:
            The updated row.

        Raises:
            LookupError: If the addon is not installed.
        """
        row = await self.get_by_id(addon_id)
        if row is None:
            raise LookupError(f"Addon {addon_id!r} is not installed.")
        if version is not None:
            row.version = version
        if enabled is not None:
            row.enabled = enabled
        if status is not None:
            row.status = status
        await self._db.flush()
        await self._db.refresh(row)
        return row

    async def delete(self, addon_id: str) -> None:
        """Remove the install row.

        Args:
            addon_id: The addon slug.
        """
        row = await self.get_by_id(addon_id)
        if row is None:
            return
        await self._db.delete(row)
        await self._db.flush()

    async def acquire_uninstall_lock(self, addon_id: str) -> None:
        """Take a transaction-scoped advisory lock for uninstall serialization.

        Args:
            addon_id: The addon slug (lock key material).
        """
        await self._db.execute(
            text("SELECT pg_advisory_xact_lock(839472, hashtext(:key))"),
            {"key": f"addon_{addon_id}"},
        )

    async def drop_addon_table(self, table: str) -> None:
        """Drop one manifest-declared addon table (allowlisted identifier).

        Args:
            table: Table name from the validated manifest.

        Raises:
            ValidationError: On any name outside the identifier allowlist.
        """
        if not TABLE_NAME_RE.match(table):
            raise ValidationError(f"Refusing to drop table {table!r}.")
        # ⚠️ Allowlisted identifier only (TABLE_NAME_RE) — never user input.
        await self._db.execute(text(f'DROP TABLE IF EXISTS "{table}"'))  # noqa: S608
