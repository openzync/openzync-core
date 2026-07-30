"""Unit tests for quick_actions_service — context-aware dashboard suggestions."""
from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

from services.quick_actions_service import QuickActionsService

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


class TestQuickActionsService:
    """Tests for QuickActionsService — get_actions with mocked repos."""

    def _make_service(
        self,
        project_count: int = 0,
        llm_provider: str | None = None,
        user_count: int = 0,
    ) -> QuickActionsService:
        """Create service with mocked repositories returning given values."""
        project_repo = AsyncMock()
        project_repo.count_active = AsyncMock(return_value=project_count)

        org_repo = AsyncMock()
        org_repo.get_llm_config = AsyncMock(
            return_value={"provider": llm_provider} if llm_provider else {},
        )

        user_repo = AsyncMock()
        user_repo.count_active = AsyncMock(return_value=user_count)

        return QuickActionsService(
            project_repo=project_repo,
            user_repo=user_repo,
            org_repo=org_repo,
        )

    async def test_new_org_shows_onboarding_actions(self) -> None:
        """Fresh org with no projects, no LLM, 1 user shows all onboarding actions."""
        service = self._make_service(
            project_count=0,
            llm_provider=None,
            user_count=1,
        )

        actions = await service.get_actions(ORG_ID)

        labels = [a["label"] for a in actions]
        assert "Create your first project" in labels
        assert "Configure LLM Provider" in labels
        assert "Invite Team Members" in labels
        assert "View Analytics" in labels
        assert "View Audit Log" in labels

    async def test_established_org_shows_view_actions(self) -> None:
        """Org with projects, LLM, and team shows 'View' actions without onboarding."""
        service = self._make_service(
            project_count=5,
            llm_provider="openai",
            user_count=3,
        )

        actions = await service.get_actions(ORG_ID)

        labels = [a["label"] for a in actions]
        assert "View Projects" in labels
        assert "Configure LLM Provider" not in labels
        assert "Invite Team Members" not in labels
        assert "View Analytics" in labels
        assert "View Audit Log" in labels

        # Find the "View Projects" action and check description
        view_proj = next(a for a in actions if a["label"] == "View Projects")
        assert "5 active projects" in view_proj["description"]

    async def test_single_project_uses_singular_form(self) -> None:
        """Exactly 1 project uses singular 'project' in the description."""
        service = self._make_service(
            project_count=1,
            llm_provider="openai",
            user_count=3,
        )

        actions = await service.get_actions(ORG_ID)
        view_proj = next(a for a in actions if a["label"] == "View Projects")
        assert "1 active project" in view_proj["description"]
        assert "projects" not in view_proj["description"]

    async def test_no_llm_config_shows_configure_llm(self) -> None:
        """Empty LLM config (no provider key) triggers the LLM setup action."""
        service = self._make_service(
            project_count=10,
            llm_provider=None,
            user_count=5,
        )

        actions = await service.get_actions(ORG_ID)
        labels = [a["label"] for a in actions]
        assert "Configure LLM Provider" in labels

    async def test_single_user_shows_invite(self) -> None:
        """Only 1 user in the org shows 'Invite Team Members'."""
        service = self._make_service(
            project_count=10,
            llm_provider="openai",
            user_count=1,
        )

        actions = await service.get_actions(ORG_ID)
        labels = [a["label"] for a in actions]
        assert "Invite Team Members" in labels

    async def test_always_has_analytics_and_audit(self) -> None:
        """View Analytics and View Audit Log are always present."""
        service = self._make_service(
            project_count=0,
            llm_provider=None,
            user_count=0,
        )

        actions = await service.get_actions(ORG_ID)
        labels = [a["label"] for a in actions]
        assert "View Analytics" in labels
        assert "View Audit Log" in labels
