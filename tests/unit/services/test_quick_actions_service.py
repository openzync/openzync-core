"""Unit tests for quick_actions_service — context-aware dashboard suggestions."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

from sqlalchemy.exc import InvalidRequestError

from repositories.organization_repository import OrganizationRepository
from repositories.project_repository import ProjectRepository
from repositories.user_repository import UserRepository
from services.quick_actions_service import QuickActionsService

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


class ConcurrencyDetectingFakeSession:
    """Fake session that raises on overlapping ``execute`` calls.

    Same technique as
    ``tests/unit/services/test_global_search_service.py``: mirrors the real
    ``AsyncSession`` contract of one in-flight operation. While an
    ``execute`` is suspended at its ``await``, a second entry raises
    ``InvalidRequestError`` — the failure Postgres raised under ``gather``.
    Returns a universal result stub: ``scalar()`` → 0 for the count legs,
    ``one_or_none()`` → row with ``llm={}`` for the LLM-config leg.
    """

    def __init__(self) -> None:
        self._in_flight = 0
        self.max_in_flight = 0

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        """Track overlap; fail like ``AsyncSession`` on concurrent use."""
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            if self._in_flight > 1:
                raise InvalidRequestError(
                    "concurrent operations are not permitted"
                )
            await asyncio.sleep(0.01)
            result = MagicMock()
            result.scalar.return_value = 0
            row = MagicMock()
            row.llm = {}
            row.llm_config = None
            row.config = None
            result.one_or_none.return_value = row
            return result
        finally:
            self._in_flight -= 1


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

    async def test_get_actions_runs_sequentially_on_single_session(self) -> None:
        """``get_actions`` never overlaps ``execute`` calls on one session.

        Regression: the three repo legs fanned out via ``asyncio.gather``
        on a single ``AsyncSession``, raising ``InvalidRequestError`` under
        real Postgres. The fake raises on overlap, so it fails on
        ``gather`` and passes on sequential awaits.
        """
        fake = ConcurrencyDetectingFakeSession()
        service = QuickActionsService(
            project_repo=ProjectRepository(db=fake),  # type: ignore[arg-type]
            user_repo=UserRepository(db=fake),  # type: ignore[arg-type]
            org_repo=OrganizationRepository(db=fake),  # type: ignore[arg-type]
        )

        actions = await service.get_actions(ORG_ID)

        assert len(actions) > 0
        assert fake.max_in_flight == 1

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
