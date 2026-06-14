"""Repository for custom instruction CRUD operations.

Follows the ``WebhookRepository`` pattern — thin data access with
``AsyncSession`` injection, typed returns, and no business logic.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.custom_instruction import CustomInstruction


class CustomInstructionRepository:
    """All database access for custom instructions.

    Every public method is async and returns domain ORM models or ``None``.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_by_scope(
        self,
        org_id: uuid.UUID,
        scope: str,
        target_id: uuid.UUID | None = None,
    ) -> list[CustomInstruction]:
        """Fetch instructions for a given scope + target.

        Args:
            org_id: The owning organization UUID.
            scope: Instruction scope (``extraction`` or ``user_summary``).
            target_id: Optional target entity UUID.  ``None`` fetches
                org-level instructions for the scope.

        Returns:
            Ordered list of matching ``CustomInstruction`` objects, ordered
            by name alphabetically.
        """
        conditions = [
            CustomInstruction.organization_id == org_id,
            CustomInstruction.scope == scope,
        ]
        if target_id is not None:
            conditions.append(CustomInstruction.target_id == target_id)
        else:
            conditions.append(CustomInstruction.target_id.is_(None))

        result = await self._db.execute(
            select(CustomInstruction).where(*conditions).order_by(CustomInstruction.name)
        )
        return list(result.scalars().all())

    async def set_by_scope(
        self,
        org_id: uuid.UUID,
        scope: str,
        target_id: uuid.UUID | None,
        instructions: list[dict],
    ) -> list[CustomInstruction]:
        """Atomically replace all instructions for a scope + target.

        Deletes all existing rows matching the key, then bulk-inserts the
        provided instruction dicts.

        Args:
            org_id: The owning organization UUID.
            scope: Instruction scope.
            target_id: Optional target entity UUID.  ``None`` replaces
                org-level instructions.
            instructions: List of ``{name, text}`` dicts to insert.

        Returns:
            The newly created ``CustomInstruction`` objects with server-side
            defaults populated (id, created_at, updated_at).
        """
        # Delete all existing for this scope+target
        delete_conditions = [
            CustomInstruction.organization_id == org_id,
            CustomInstruction.scope == scope,
        ]
        if target_id is not None:
            delete_conditions.append(CustomInstruction.target_id == target_id)
        else:
            delete_conditions.append(CustomInstruction.target_id.is_(None))

        await self._db.execute(
            delete(CustomInstruction).where(*delete_conditions)
        )

        # Bulk insert the new ones
        new_instructions: list[CustomInstruction] = []
        for instr in instructions:
            obj = CustomInstruction(
                organization_id=org_id,
                scope=scope,
                target_id=target_id,
                name=instr["name"],
                text=instr["text"],
            )
            self._db.add(obj)
            new_instructions.append(obj)

        await self._db.flush()

        # Refresh so server-side defaults (id, created_at, updated_at)
        # are populated into the Python objects.
        for obj in new_instructions:
            await self._db.refresh(obj)

        return new_instructions

    async def delete_by_scope(
        self,
        org_id: uuid.UUID,
        scope: str,
        target_id: uuid.UUID | None = None,
    ) -> None:
        """Delete all instructions for a scope + target.

        Args:
            org_id: The owning organization UUID.
            scope: Instruction scope.
            target_id: Optional target entity UUID.  ``None`` deletes
                org-level instructions for the given scope.
        """
        del_conds = [
            CustomInstruction.organization_id == org_id,
            CustomInstruction.scope == scope,
        ]
        if target_id is not None:
            del_conds.append(CustomInstruction.target_id == target_id)
        else:
            del_conds.append(CustomInstruction.target_id.is_(None))

        await self._db.execute(delete(CustomInstruction).where(*del_conds))
        await self._db.flush()
