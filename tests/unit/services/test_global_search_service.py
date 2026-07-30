"""Unit tests for GlobalSearchService — cross-resource search orchestration.

All DB interactions are mocked at the service boundary — no real I/O occurs.
Each private query method is replaced with an AsyncMock returning controlled data.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock
from uuid import UUID

import pytest

from schemas.search import GlobalSearchItem
from services.global_search_service import GlobalSearchService


@pytest.mark.unit
class TestGlobalSearchService:
    """Unit tests for ``GlobalSearchService`` — cross-resource search."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(self) -> tuple[GlobalSearchService, AsyncMock]:
        """Create a GlobalSearchService with mocked DB session."""
        mock_db = AsyncMock()
        service = GlobalSearchService(db=mock_db, org_id=self.ORG_ID, user_id=self.USER_ID)
        return service, mock_db

    @staticmethod
    def _make_db_row(
        row_id: str | UUID,
        name: str = "test",
        email: str | None = None,
        description: str | None = None,
        external_id: str | None = None,
        project_name: str | None = None,
        project_id: str | None = None,
    ) -> MagicMock:
        """Build a MagicMock that mimics a SQLAlchemy Row for attribute access."""
        row = MagicMock()
        row.id = str(row_id)
        row.name = name
        row.email = email
        row.description = description
        row.external_id = external_id
        row.project_name = project_name
        row.project_id = project_id
        return row

    # ── search — all three types ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_search_returns_results_from_all_types(self) -> None:
        """``search`` merges results from projects, users, and sessions."""
        service, mock_db = self._make_service()

        # Mock _db.execute to return different results for each query
        # We mock the private methods instead since they're the composition units
        mock_project_item = GlobalSearchItem(
            type="project", id="p1", label="Proj A",
            subtitle="desc", href="/projects/p1",
        )
        mock_user_item = GlobalSearchItem(
            type="user", id="u1", label="user@example.com",
            subtitle="User One", href="/users/u1",
        )
        mock_session_item = GlobalSearchItem(
            type="session", id="s1", label="SESS-001",
            subtitle="Proj A", href="/projects/p1/sessions/s1",
        )

        service._search_projects = AsyncMock(return_value=[mock_project_item])
        service._search_users = AsyncMock(return_value=[mock_user_item])
        service._search_sessions = AsyncMock(return_value=[mock_session_item])

        results = await service.search("test")

        assert len(results) == 3
        types = {r.type for r in results}
        assert types == {"project", "user", "session"}
        service._search_projects.assert_awaited_once()
        service._search_users.assert_awaited_once()
        service._search_sessions.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_no_matching_results(self) -> None:
        """``search`` returns an empty list when nothing matches."""
        service, mock_db = self._make_service()

        service._search_projects = AsyncMock(return_value=[])
        service._search_users = AsyncMock(return_value=[])
        service._search_sessions = AsyncMock(return_value=[])

        results = await service.search("zzzzzzz")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_respects_limit(self) -> None:
        """``search`` caps results at the requested limit."""
        service, mock_db = self._make_service()

        # Return more results than the limit allows
        projects = [
            GlobalSearchItem(type="project", id=f"p{i}", label=f"P{i}",
                             subtitle=None, href=f"/projects/p{i}")
            for i in range(5)
        ]
        users = [
            GlobalSearchItem(type="user", id=f"u{i}", label=f"u{i}@e.com",
                             subtitle=None, href=f"/users/u{i}")
            for i in range(5)
        ]
        sessions = [
            GlobalSearchItem(type="session", id=f"s{i}", label=f"S{i}",
                             subtitle="Proj", href=f"/proj/s/{i}")
            for i in range(5)
        ]

        service._search_projects = AsyncMock(return_value=projects)
        service._search_users = AsyncMock(return_value=users)
        service._search_sessions = AsyncMock(return_value=sessions)

        results = await service.search("test", limit=5)
        assert len(results) <= 5

    @pytest.mark.asyncio
    async def test_search_results_sorted(self) -> None:
        """``search`` results are sorted by type then label."""
        service, mock_db = self._make_service()

        service._search_projects = AsyncMock(return_value=[
            GlobalSearchItem(type="project", id="p2", label="Beta",
                             subtitle=None, href="/p2"),
            GlobalSearchItem(type="project", id="p1", label="Alpha",
                             subtitle=None, href="/p1"),
        ])
        service._search_users = AsyncMock(return_value=[
            GlobalSearchItem(type="user", id="u1", label="b@e.com",
                             subtitle=None, href="/u1"),
        ])
        service._search_sessions = AsyncMock(return_value=[])

        results = await service.search("test", limit=10)
        # Projects come before users, and within projects they're sorted by label
        assert results[0].label == "Alpha"
        assert results[1].label == "Beta"
        assert results[2].label == "b@e.com"

    # ── _search_projects — raw DB query ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_search_projects_no_matches(self) -> None:
        """``_search_projects`` returns empty list when no projects match."""
        service, mock_db = self._make_service()

        mock_db.execute.return_value.all.return_value = []

        results = await service._search_projects("%nothing%", 10)
        assert results == []

    # ── _search_users — label precedence ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_search_users_email_preferred_as_label(self) -> None:
        """``_search_users`` uses email as label when both name and email exist."""
        service, mock_db = self._make_service()

        row = self._make_db_row("u1", name="Alice", email="alice@example.com")
        mock_db.execute.return_value = [row]

        results = await service._search_users("%alice%", 10)
        assert len(results) == 1
        assert results[0].label == "alice@example.com"
        assert results[0].subtitle == "Alice"

    @pytest.mark.asyncio
    async def test_search_users_email_only(self) -> None:
        """``_search_users`` uses email as label when name is missing."""
        service, mock_db = self._make_service()

        row = self._make_db_row("u1", name=None, email="anon@example.com")
        mock_db.execute.return_value = [row]

        results = await service._search_users("%anon%", 10)
        assert len(results) == 1
        assert results[0].label == "anon@example.com"
        assert results[0].subtitle is None
