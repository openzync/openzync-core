"""Unit tests for GlobalSearchService — cross-resource search orchestration.

All DB interactions are mocked at the service boundary — no real I/O occurs.
Each private query method is replaced with an AsyncMock returning controlled data.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy.exc import InvalidRequestError

from schemas.search import GlobalSearchItem
from services.global_search_service import GlobalSearchService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class ConcurrencyDetectingFakeSession:
    """Fake session that raises on overlapping ``execute`` calls.

    Mirrors the real ``AsyncSession`` contract of one in-flight operation:
    while an ``execute`` is suspended at its ``await``, a second entry
    raises ``InvalidRequestError`` — the same failure Postgres raised
    under the old ``asyncio.gather`` fan-out.
    """

    def __init__(self) -> None:
        self._in_flight = 0
        self.max_in_flight = 0

    async def execute(
        self, stmt: Any, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """Track overlap; fail like ``AsyncSession`` on concurrent use."""
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            if self._in_flight > 1:
                raise InvalidRequestError("concurrent operations are not permitted")
            await asyncio.sleep(0.01)
            return []
        finally:
            self._in_flight -= 1


@pytest.mark.unit
class TestGlobalSearchService:
    """Unit tests for ``GlobalSearchService`` — cross-resource search."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(self) -> tuple[GlobalSearchService, AsyncMock]:
        """Create a GlobalSearchService with mocked DB session."""
        mock_db = AsyncMock()
        service = GlobalSearchService(
            db=mock_db, org_id=self.ORG_ID, user_id=self.USER_ID
        )
        return service, mock_db

    @staticmethod
    def _make_db_row(
        row_id: str | UUID,
        name: str | None = "test",
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

    @pytest.mark.asyncio
    async def test_search_runs_sequentially_on_single_session(self) -> None:
        """``search`` never overlaps ``execute`` calls on one session.

        Regression: the three legs fanned out via ``asyncio.gather`` on a
        single ``AsyncSession``, raising ``InvalidRequestError`` under real
        Postgres.  The fake raises on overlap, so it fails on ``gather``
        and passes on sequential awaits.
        """
        fake = ConcurrencyDetectingFakeSession()
        service = GlobalSearchService(
            db=cast("AsyncSession", fake), org_id=self.ORG_ID, user_id=self.USER_ID
        )

        results = await service.search("test", limit=9)

        assert results == []
        assert fake.max_in_flight == 1

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

    @pytest.mark.asyncio
    async def test_search_users_nameless_emailless_falls_back_to_external_id(
        self,
    ) -> None:
        """Nameless + emailless row labels by ``external_id`` — no 500.

        Regression: with neither name nor email, the old label expression
        produced ``None`` and ``GlobalSearchItem`` raised ``ValidationError``
        (500 at the API). The label must fall back to ``external_id``.
        """
        service, mock_db = self._make_service()

        row = self._make_db_row(
            "u1", name=None, email=None, external_id="ext-123"
        )
        mock_db.execute.return_value = [row]

        results = await service._search_users("%ext-123%", 10)
        assert len(results) == 1
        assert results[0].label == "ext-123"
        assert results[0].subtitle is None

    @pytest.mark.asyncio
    async def test_search_users_all_null_falls_back_to_id(self) -> None:
        """Row with name=email=external_id=None labels by ``str(id)``.

        Regression: the label chain ends with ``or str(row.id)`` so a
        fully anonymous row can never produce a ``None`` label (which
        would raise ``ValidationError`` in ``GlobalSearchItem``).
        """
        service, mock_db = self._make_service()

        row = self._make_db_row(
            "u1", name=None, email=None, external_id=None
        )
        mock_db.execute.return_value = [row]

        results = await service._search_users("%u1%", 10)
        assert len(results) == 1
        assert results[0].label == str(row.id)
        assert results[0].subtitle is None
