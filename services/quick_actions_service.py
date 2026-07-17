"""Quick actions service — context-aware action suggestions for the dashboard.

Determines which quick actions to show based on the organization's current
state (projects, LLM config, users).  Called by the overview page router.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from repositories.organization_repository import OrganizationRepository
from repositories.project_repository import ProjectRepository
from repositories.user_repository import UserRepository


class QuickActionsService:
    """Generates context-aware quick actions for the dashboard overview.

    Args:
        project_repo: Repository for project queries.
        user_repo: Repository for user queries.
        org_repo: Repository for organization queries.
    """

    def __init__(
        self,
        project_repo: ProjectRepository,
        user_repo: UserRepository,
        org_repo: OrganizationRepository,
    ) -> None:
        self._project_repo = project_repo
        self._user_repo = user_repo
        self._org_repo = org_repo

    async def get_actions(self, org_id: UUID) -> list[dict]:
        """Build a prioritized list of quick actions based on org state.

        Args:
            org_id: The organization UUID.

        Returns:
            A list of action dicts, each with ``label``, ``href``, ``icon``,
            and optional ``description`` keys.
        """
        actions: list[dict] = []

        # All three queries are independent — run in parallel
        project_count, llm_config, user_count = await asyncio.gather(
            self._project_repo.count_active(org_id),
            self._org_repo.get_llm_config(org_id),
            self._user_repo.count_active(org_id),
        )

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
        if not llm_config.get("provider"):
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
            "href": "/analytics",
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
