"""Global search service — cross-resource search across org-scoped entities.

Runs three sequential ILIKE queries (projects, users, sessions) scoped to the
authenticated user's organization and membership, returning a flat sorted list.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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

    # ── Private query methods ─────────────────────────────────────────────

    async def _search_projects(self, pattern: str, limit: int) -> list[GlobalSearchItem]:
        """Search projects the user is a member of."""
        stmt = text("""
            SELECT p.id, p.name, p.description, p.created_at
            FROM projects p
            JOIN project_members pm ON p.id = pm.project_id
            WHERE p.organization_id = :org_id
              AND pm.user_id = :user_id
              AND p.is_archived = false
              AND p.name ILIKE :pattern
            LIMIT :limit
        """)
        rows = await self._db.execute(
            stmt,
            {"org_id": str(self._org_id), "user_id": str(self._user_id), "pattern": pattern, "limit": limit},
        )
        items: list[GlobalSearchItem] = []
        for row in rows:
            item = GlobalSearchItem(
                type="project",
                id=str(row.id),
                label=row.name,
                subtitle=row.description,
                href=f"/projects/{row.id}",
            )
            items.append(item)
            created = row.created_at.isoformat() if row.created_at else ""
            self._recent_keys[item.id] = created
        return items

    async def _search_users(self, pattern: str, limit: int) -> list[GlobalSearchItem]:
        """Search users in the same organization."""
        stmt = text("""
            SELECT id, name, email, external_id, created_at
            FROM users
            WHERE organization_id = :org_id
              AND is_deleted = false
              AND (name ILIKE :pattern OR email ILIKE :pattern OR external_id ILIKE :pattern)
            LIMIT :limit
        """)
        rows = await self._db.execute(
            stmt,
            {"org_id": str(self._org_id), "pattern": pattern, "limit": limit},
        )
        results: list[GlobalSearchItem] = []
        for row in rows:
            # Prefer email as label when both name and email exist
            if row.email:
                label = row.email
                subtitle = row.name
            else:
                label = row.name or row.external_id or str(row.id)
                subtitle = None
            item = GlobalSearchItem(
                type="user",
                id=str(row.id),
                label=label,
                subtitle=subtitle,
                href=f"/users/{row.id}",
            )
            results.append(item)
            created = row.created_at.isoformat() if row.created_at else ""
            self._recent_keys[item.id] = created
        return results

    async def _search_sessions(self, pattern: str, limit: int) -> list[GlobalSearchItem]:
        """Search sessions within projects the user is a member of."""
        stmt = text("""
            SELECT s.id, s.external_id, s.project_id, p.name as project_name,
                   s.created_at
            FROM sessions s
            JOIN projects p ON s.project_id = p.id
            JOIN project_members pm ON p.id = pm.project_id
            WHERE p.organization_id = :org_id
              AND pm.user_id = :user_id
              AND s.is_deleted = false
              AND s.external_id ILIKE :pattern
            LIMIT :limit
        """)
        rows = await self._db.execute(
            stmt,
            {"org_id": str(self._org_id), "user_id": str(self._user_id), "pattern": pattern, "limit": limit},
        )
        items: list[GlobalSearchItem] = []
        for row in rows:
            item = GlobalSearchItem(
                type="session",
                id=str(row.id),
                label=row.external_id,
                subtitle=row.project_name,
                href=f"/projects/{row.project_id}/sessions/{row.id}",
            )
            items.append(item)
            created = row.created_at.isoformat() if row.created_at else ""
            self._recent_keys[item.id] = created
        return items
