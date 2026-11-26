"""Quick actions service — context-aware action suggestions for the dashboard.

Determines which quick actions to show based on the organization's current
state (projects, LLM config, users).  Called by the overview page router.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.org_config import get_org_config
from repositories.organization_repository import OrganizationRepository
from repositories.project_repository import ProjectRepository
from repositories.user_repository import UserRepository


class QuickActionsService:
    """Generates context-aware quick actions for the dashboard overview.

    Args:
        project_repo: Repository for project queries.
        user_repo: Repository for user queries.
        org_repo: Repository for organization queries.
        db: Async session for canonical per-user config resolution
            (``core.org_config`` reads flat ``users.config_override``).
            Falls back to ``org_repo.session`` when omitted.
        redis: Optional async Redis client for cache-first config reads.
            ``None`` skips the cache (DB-authoritative).
    """

    def __init__(
        self,
        project_repo: ProjectRepository,
        user_repo: UserRepository,
        org_repo: OrganizationRepository,
        db: AsyncSession | None = None,
        redis: Any | None = None,
    ) -> None:
        self._project_repo = project_repo
        self._user_repo = user_repo
        self._org_repo = org_repo
        self._db = db if db is not None else org_repo.session
        self._redis = redis

    async def get_actions(self, user_id: UUID) -> list[dict]:
        """Build a prioritized list of quick actions based on org state.

        Args:
            user_id: The organization UUID.

        Returns:
            A list of action dicts, each with ``label``, ``href``, ``icon``,
            and optional ``description`` keys.
        """
        actions: list[dict] = []

        # One AsyncSession allows only one in-flight query — sequential, not gather.
        project_count = await self._project_repo.count_active(user_id)
        # Canonical per-user config (flat users.config_override shape,
        # cache-first) — never the legacy nested organizations.config.
        org_config = await get_org_config(
            user_id, redis=self._redis, db=self._db
        )
        user_count = await self._user_repo.count_active(user_id)

        # 1. Projects
        if project_count == 0:
            actions.append({
                "label": "Create your first project",
                "href": "/projects",
                "icon": "folder-kanban",
                "description": "Projects organize sessions, memory, and graph data",
            })
        else:
            actions.append({
                "label": "View Projects",
                "href": "/projects",
                "icon": "folder-kanban",
                "description": f"{project_count} active project{'s' if project_count != 1 else ''}",
            })

        # 2. LLM config
        if not org_config.llm_backend:
            actions.append({
                "label": "Configure LLM Provider",
                "href": "/settings/org-config/llm",
                "icon": "brain-circuit",
                "description": "Set up your LLM provider to start using sessions",
            })

        # 3. Team size
        if user_count < 2:
            actions.append({
                "label": "Invite Team Members",
                "href": "/users",
                "icon": "users",
                "description": "Collaborate with your team",
            })

        # 4. Always show
        actions.append({
            "label": "View Analytics",
            "href": "/overview",
            "icon": "bar-chart-3",
            "description": "Usage trends and metrics",
        })
        actions.append({
            "label": "View Audit Log",
            "href": "/audit",
            "icon": "shield",
            "description": "Track changes across your organization",
        })

        return actions
