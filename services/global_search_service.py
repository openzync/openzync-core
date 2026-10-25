"""Global search service — cross-resource search across org-scoped entities.

Fans out to three repository ILIKE legs (projects, users, sessions) scoped
to the authenticated user's organization and membership, then merges and
sorts — the service keeps merge/sort only, no SQL.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from repositories.project_repository import ProjectRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from schemas.search import GlobalSearchItem

logger = logging.getLogger(__name__)


class GlobalSearchService:
    """Search across projects, users, and sessions within an organization.

    Queries are scoped by ``org_id`` and (for projects/sessions) by
    ``user_id`` membership, so results only include resources the
    authenticated user can access.
    """

    def __init__(self, db: AsyncSession, org_id: UUID, user_id: UUID) -> None:
        self._db = db
        self._org_id = org_id
        self._user_id = user_id
        self._recent_keys: dict[str, str] = {}
        """Sort-only ``id → created_at ISO`` map for ``sort=recent``."""

    async def search(
        self, query: str, limit: int = 10, sort: str = "relevance"
    ) -> list[GlobalSearchItem]:
        """Run a global search across projects, users, and sessions.

        Args:
            query: The search query string (used in ILIKE patterns).
            limit: Maximum total results across all entity types.
            sort: ``"relevance"`` (type then label alphabetically) or
                ``"recent"`` (``created_at DESC`` across all types,
                applied server-side; ``created_at`` is sort-only and
                never leaves the server in the response shape).

        Returns:
            A flat list of :class:`GlobalSearchItem` sorted by type then
            label alphabetically (relevance) or by recency (recent).
        """
        if sort not in ("relevance", "recent"):
            from core.exceptions import ValidationError

            raise ValidationError(f"Invalid sort: {sort!r}")
        pattern = f"%{query}%"
        per_type = max(1, limit // 3)

        # One AsyncSession allows only one in-flight operation, so the
        # three legs must run sequentially, not via asyncio.gather.
        p_results = await self._search_projects(pattern, per_type)
        u_results = await self._search_users(pattern, per_type)
        s_results = await self._search_sessions(pattern, per_type)

        all_results: list[GlobalSearchItem] = [*p_results, *u_results, *s_results]
        if sort == "recent":
            all_results.sort(
                key=lambda r: (self._recent_keys.get(r.id, ""), r.id),
                reverse=True,
            )
            return all_results[:limit]
        all_results.sort(key=lambda r: (r.type, r.label))
        return all_results[:limit]

    # ── Private query methods (service keeps merge/sort only) ─────────────

    async def _search_projects(
        self, pattern: str, limit: int
    ) -> list[GlobalSearchItem]:
        """Search projects the user is a member of."""
        rows = await ProjectRepository(self._db).search_member_projects(
            self._org_id, self._user_id, pattern, limit
        )
        items: list[GlobalSearchItem] = []
        for row in rows:
            item = GlobalSearchItem(
                type="project",
                id=row["id"],
                label=row["name"],
                subtitle=row["description"],
                href=f"/projects/{row['id']}",
            )
            items.append(item)
            created = row["created_at"]
            self._recent_keys[item.id] = created.isoformat() if created else ""
        return items

    async def _search_users(self, pattern: str, limit: int) -> list[GlobalSearchItem]:
        """Search users in the same organization."""
        rows = await UserRepository(self._db).search_by_pattern(
            self._org_id, pattern, limit
        )
        results: list[GlobalSearchItem] = []
        for row in rows:
            # Prefer email as label when both name and email exist
            if row["email"]:
                label = row["email"]
                subtitle = row["name"]
            else:
                label = row["name"] or row["external_id"] or row["id"]
                subtitle = None
            item = GlobalSearchItem(
                type="user",
                id=row["id"],
                label=label,
                subtitle=subtitle,
                href=f"/users/{row['id']}",
            )
            results.append(item)
            created = row["created_at"]
            self._recent_keys[item.id] = created.isoformat() if created else ""
        return results

    async def _search_sessions(
        self, pattern: str, limit: int
    ) -> list[GlobalSearchItem]:
        """Search sessions within projects the user is a member of."""
        rows = await SessionRepository(self._db).search_member_sessions(
            self._org_id, self._user_id, pattern, limit
        )
        items: list[GlobalSearchItem] = []
        for row in rows:
            item = GlobalSearchItem(
                type="session",
                id=row["id"],
                label=row["external_id"],
                subtitle=row["project_name"],
                href=f"/projects/{row['project_id']}/sessions/{row['id']}",
            )
            items.append(item)
            created = row["created_at"]
            self._recent_keys[item.id] = created.isoformat() if created else ""
        return items
