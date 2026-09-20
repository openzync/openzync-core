"""Project pin repository — all database access for ProjectPins.

Every query is scoped to an ``organization_id``. The repository returns
ORM models only — no business logic, no schema construction.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Uuid, delete, func, insert, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.sorting import SortSpec, resolve_order_by
from models.project import Project
from models.project_pin import ProjectPin

PINNED_PROJECT_SORTABLE_COLUMNS = {
    "pinned_at": ProjectPin.pinned_at,
    "name": Project.name,
    "created_at": Project.created_at,
    "updated_at": Project.updated_at,
}
"""Sortable columns for pinned projects (default pinned_at/desc)."""


class ProjectPinRepository:
    """All database access for project pins.

    Args:
        db: An async SQLAlchemy session (request-scoped).
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_pinned_ids(self, organization_id: UUID, user_id: UUID) -> set[UUID]:
        """Return the IDs of all projects pinned by a user in an org.

        Args:
            organization_id: Tenant scope.
            user_id: The user whose pins to fetch.

        Returns:
            A set of pinned project UUIDs (empty if none).
        """
        result = await self._db.execute(
            select(ProjectPin.project_id).where(
                ProjectPin.organization_id == organization_id,
                ProjectPin.user_id == user_id,
            )
        )
        return set(result.scalars().all())

    async def count_for_user(self, organization_id: UUID, user_id: UUID) -> int:
        """Count how many projects a user has pinned in an org.

        Args:
            organization_id: Tenant scope.
            user_id: The user whose pins to count.

        Returns:
            Pin count (0 if none).
        """
        result = await self._db.execute(
            select(func.count(ProjectPin.id)).where(
                ProjectPin.organization_id == organization_id,
                ProjectPin.user_id == user_id,
            )
        )
        return result.scalar() or 0

    async def is_pinned(
        self, organization_id: UUID, user_id: UUID, project_id: UUID
    ) -> bool:
        """Check whether a user has pinned a project.

        Args:
            organization_id: Tenant scope.
            user_id: The user to check.
            project_id: The project to check.

        Returns:
            ``True`` if a pin row exists, ``False`` otherwise.
        """
        result = await self._db.execute(
            select(ProjectPin.id).where(
                ProjectPin.organization_id == organization_id,
                ProjectPin.user_id == user_id,
                ProjectPin.project_id == project_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def pin(
        self, organization_id: UUID, user_id: UUID, project_id: UUID
    ) -> ProjectPin:
        """Create a pin row (idempotent on duplicate).

        A concurrent duplicate insert raises ``IntegrityError`` on the
        ``uq_project_pins_user_project`` constraint — only the savepoint
        rolls back (the surrounding request transaction is untouched) and
        the existing row is returned instead, so callers never observe a
        500 for a double-pin race.

        Args:
            organization_id: Tenant scope.
            user_id: The user pinning.
            project_id: The project to pin.

        Returns:
            The new or pre-existing ProjectPin.
        """
        pin = ProjectPin(
            organization_id=organization_id,
            user_id=user_id,
            project_id=project_id,
        )
        try:
            async with self._db.begin_nested():
                self._db.add(pin)
                await self._db.flush()
        except IntegrityError:
            result = await self._db.execute(
                select(ProjectPin).where(
                    ProjectPin.organization_id == organization_id,
                    ProjectPin.user_id == user_id,
                    ProjectPin.project_id == project_id,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is None:  # pragma: no cover — lost race, row vanished
                raise
            return existing
        await self._db.refresh(pin)
        return pin

    async def pin_if_under_limit(
        self,
        organization_id: UUID,
        user_id: UUID,
        project_id: UUID,
        limit: int,
    ) -> ProjectPin | None:
        """Pin a project only if the user is under the pin limit.

        A transaction-scoped advisory lock on ``(organization_id, user_id)``
        serializes concurrent pins for the same user (READ COMMITTED alone
        lets two ``INSERT ... WHERE count < limit`` statements both see the
        same count), followed by a single ``INSERT ... SELECT ... WHERE
        count < limit RETURNING`` statement. A concurrent duplicate insert
        (same user+project) hits ``uq_project_pins_user_project`` — only
        the savepoint rolls back and the pre-existing row is returned
        (idempotent).

        Args:
            organization_id: Tenant scope.
            user_id: The user pinning.
            project_id: The project to pin.
            limit: Maximum pins per user per org (passed by the service).

        Returns:
            The new (or pre-existing, on a double-pin race) ProjectPin,
            or ``None`` when the limit is already reached.
        """
        # ⚠️ RACE CONDITION FIX: without this lock, two concurrent pins at
        # count=2 both snapshot count=2 under READ COMMITTED (neither sees
        # the other's uncommitted insert) and both insert. The xact lock is
        # held until the request session commits, so the second pin blocks,
        # then sees count=3 and returns None.
        await self._db.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtext(f"project_pins:{organization_id}:{user_id}")
                )
            )
        )
        pin_count = (
            select(func.count(ProjectPin.id))
            .where(
                ProjectPin.organization_id == organization_id,
                ProjectPin.user_id == user_id,
            )
            .scalar_subquery()
        )
        stmt = (
            insert(ProjectPin)
            .from_select(
                ["organization_id", "user_id", "project_id"],
                select(
                    literal(organization_id, type_=Uuid),
                    literal(user_id, type_=Uuid),
                    literal(project_id, type_=Uuid),
                ).where(pin_count < limit),
            )
            .returning(ProjectPin)
        )
        try:
            async with self._db.begin_nested():
                result = await self._db.execute(stmt)
                return result.scalar_one_or_none()
        except IntegrityError:
            result = await self._db.execute(
                select(ProjectPin).where(
                    ProjectPin.organization_id == organization_id,
                    ProjectPin.user_id == user_id,
                    ProjectPin.project_id == project_id,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is None:  # pragma: no cover — lost race, row vanished
                raise
            return existing

    async def unpin(
        self, organization_id: UUID, user_id: UUID, project_id: UUID
    ) -> bool:
        """Delete a pin row.

        Args:
            organization_id: Tenant scope.
            user_id: The user unpinning.
            project_id: The project to unpin.

        Returns:
            ``True`` if a row was deleted, ``False`` if none existed.
        """
        result = await self._db.execute(
            delete(ProjectPin)
            .where(
                ProjectPin.organization_id == organization_id,
                ProjectPin.user_id == user_id,
                ProjectPin.project_id == project_id,
            )
            .returning(ProjectPin.id)
        )
        await self._db.flush()
        return result.scalar_one_or_none() is not None

    async def delete_for_project(self, project_id: UUID) -> int:
        """Delete every user's pin on a project (e.g. on archive).

        Args:
            project_id: The project whose pins to remove.

        Returns:
            Number of pin rows deleted.
        """
        result = await self._db.execute(
            delete(ProjectPin)
            .where(ProjectPin.project_id == project_id)
            .returning(ProjectPin.id)
        )
        await self._db.flush()
        return len(list(result.scalars().all()))

    async def list_pinned_projects(
        self,
        organization_id: UUID,
        user_id: UUID,
        limit: int = 50,
        offset: int = 0,
        sort: SortSpec | None = None,
    ) -> list[Project]:
        """List a user's pinned projects, most recently pinned first.

        Archived projects are excluded even if a pin row still exists.
        Default ``pinned_at/desc``; whitelist ``pinned_at``, ``name``,
        ``created_at``, ``updated_at``.

        Args:
            organization_id: Tenant scope.
            user_id: The user whose pinned projects to list.
            limit: Maximum results per page (capped at 200).
            offset: Number of results to skip.
            sort: Validated sort spec.

        Returns:
            A list of Project ORM instances ordered by ``pinned_at`` DESC
            by default.
        """
        effective_limit = min(limit, 200)
        spec = sort if sort is not None else SortSpec()
        req_sort, req_dir = spec.effective("pinned_at", "desc")
        result = await self._db.execute(
            select(Project)
            .join(ProjectPin, Project.id == ProjectPin.project_id)
            .where(
                ProjectPin.organization_id == organization_id,
                ProjectPin.user_id == user_id,
                Project.organization_id == organization_id,
                Project.is_archived.is_(False),
            )
            .order_by(
                *resolve_order_by(
                    PINNED_PROJECT_SORTABLE_COLUMNS,
                    Project.id,
                    req_sort,
                    req_dir,
                    default_sort_by="pinned_at",
                    default_dir="desc",
                )
            )
            .limit(effective_limit)
            .offset(offset)
        )
        return list(result.scalars().all())
