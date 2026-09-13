"""Unit tests for the admin stats router.

Tests cover endpoints under ``/v1/admin/stats``:
- ``GET /org`` — organization aggregate statistics
- ``GET /usage`` — daily usage trends with optional days param

These endpoints query the DB directly via ``db.execute()`` calls from the
router helper functions.  We mock ``db.execute`` with pre-built result objects
that mimic what SQLAlchemy async returns.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from routers.admin_stats import router

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture(autouse=True)
def _stub_permission_gate() -> None:
    """Stub the permission gate for every test in this file.

    The router gates with ``require_permission("members:read")`` — a
    closure created at router import time that cannot be keyed in
    ``dependency_overrides``.  Patching ``dependencies.auth._check_permission``
    (the shared decision function) stubs the gate while keeping the
    ``require_org_id`` chain intact.  The real gate matrix is covered by
    ``test_admin_gate_matrix.py``.
    """
    with patch("dependencies.auth._check_permission", new=AsyncMock()):
        yield


class _MockScalarResult:
    """Simulates a SQLAlchemy scalar result."""

    def __init__(self, scalar_value: int) -> None:
        self._val = scalar_value

    def scalar(self) -> int | None:
        return self._val


class _MockRow:
    """Simulates a single row from a SQLAlchemy result with named attributes."""

    def __init__(self, date_str: str, count: int) -> None:
        self.date = _MockDate(date_str)
        self.count = count


class _MockDate:
    """Simulates a date-truncated column with a .date() method."""

    def __init__(self, date_str: str) -> None:
        self._date_str = date_str

    def date(self) -> str:
        return self._date_str


class _MockStreamResult:
    """Simulates a SQLAlchemy result that supports iteration for stream results."""

    def __init__(self, rows: list | None = None) -> None:
        self._rows = rows or []

    def __aiter__(self):  # noqa: ANN201
        return iter(self._rows).__aiter__()

    def __iter__(self):  # noqa: ANN201
        return iter(self._rows)


def _build_app(mock_db: AsyncMock) -> FastAPI:
    """Create a minimal FastAPI app with the stats router."""
    app = FastAPI()
    app.include_router(router)

    from dependencies.auth import get_dashboard_user, require_org_id
    from dependencies.db import get_db

    app.dependency_overrides = {}

    async def _override_get_db() -> AsyncMock:
        return mock_db

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
    app.dependency_overrides[get_dashboard_user] = lambda: str(USER_ID)

    @app.middleware("http")
    async def _mock_auth(request: Request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    return app


@pytest.fixture
def mock_db() -> AsyncMock:
    """Return an AsyncMock that simulates an async database session."""
    m = AsyncMock(spec=AsyncSession)
    return m


@pytest.fixture
async def client(mock_db: AsyncMock) -> AsyncClient:  # noqa: ANN201
    app = _build_app(mock_db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── GET /org — organization aggregate stats ──────────────────────────────────


class TestGetOrgStats:
    """GET /v1/admin/stats/org — aggregate counts for the dashboard."""

    async def test_returns_org_stats(
        self, client: AsyncClient, mock_db: AsyncMock
    ) -> None:
        """Should return aggregate counts for all six metric categories."""
        # get_org_stats runs 6 count queries in sequence: episodes,
        # sessions, facts, extractions, observations, classifications.
        mock_db.execute.side_effect = [
            _MockScalarResult(100),  # episodes
            _MockScalarResult(25),  # sessions
            _MockScalarResult(50),  # facts
            _MockScalarResult(7),  # extractions
            _MockScalarResult(30),  # observations
            _MockScalarResult(12),  # classifications
        ]

        response = await client.get("/v1/admin/stats/org")
        assert response.status_code == 200
        body = response.json()

        assert body["organization_id"] == str(ORG_ID)
        assert body["total_episodes"] == 100
        assert body["total_sessions"] == 25
        assert body["total_facts"] == 50
        assert body["total_extractions"] == 7
        assert body["total_observations"] == 30
        assert body["total_classifications"] == 12
        # Verify all 6 execute calls were made
        assert mock_db.execute.await_count == 6

    async def test_handles_zero_counts(
        self, client: AsyncClient, mock_db: AsyncMock
    ) -> None:
        """Should return zeroes when no data exists in the org."""
        mock_db.execute.side_effect = [
            _MockScalarResult(0),  # episodes
            _MockScalarResult(0),  # sessions
            _MockScalarResult(0),  # facts
            _MockScalarResult(0),  # extractions
            _MockScalarResult(0),  # observations
            _MockScalarResult(0),  # classifications
        ]
        response = await client.get("/v1/admin/stats/org")
        assert response.status_code == 200
        body = response.json()
        assert body["total_episodes"] == 0
        assert body["total_sessions"] == 0
        assert body["total_facts"] == 0
        assert body["total_extractions"] == 0
        assert body["total_observations"] == 0
        assert body["total_classifications"] == 0

    async def test_handles_none_scalar(
        self, client: AsyncClient, mock_db: AsyncMock
    ) -> None:
        """Should handle when scalar() returns None."""
        mock_db.execute.side_effect = [
            _MockScalarResult(None),  # episodes → 0
            _MockScalarResult(None),  # sessions → 0
            _MockScalarResult(1),  # facts
            _MockScalarResult(None),  # extractions → 0
            _MockScalarResult(5),  # observations
            _MockScalarResult(None),  # classifications → 0
        ]
        response = await client.get("/v1/admin/stats/org")
        assert response.status_code == 200
        body = response.json()
        assert body["total_episodes"] == 0
        assert body["total_sessions"] == 0
        assert body["total_facts"] == 1
        assert body["total_extractions"] == 0
        assert body["total_observations"] == 5
        assert body["total_classifications"] == 0


# ── GET /usage — daily usage trends ──────────────────────────────────────────


class TestGetUsageStats:
    """GET /v1/admin/stats/usage — daily message and session counts."""

    async def test_returns_usage_stats(
        self, client: AsyncClient, mock_db: AsyncMock
    ) -> None:
        """Should return merged daily counts across all eight categories."""
        # The /usage endpoint runs 8 queries in sequence:
        #   episodes, sessions, facts, extractions, observations,
        #   classifications, nodes, edges.
        # Each result is iterable (supports `for row in result`).

        class _IterResult:
            """Simulates a result that can be iterated with for..in."""

            def __init__(self, rows: list):
                self._rows = rows

            def __iter__(self):  # noqa: ANN201
                return iter(self._rows)

        mock_db.execute.side_effect = [
            _IterResult(
                [_MockRow("2026-07-28", 20), _MockRow("2026-07-27", 15)]
            ),  # episodes
            _IterResult(
                [_MockRow("2026-07-28", 5), _MockRow("2026-07-27", 3)]
            ),  # sessions
            _IterResult([_MockRow("2026-07-28", 7)]),  # facts
            _IterResult([_MockRow("2026-07-28", 2)]),  # extractions
            _IterResult([_MockRow("2026-07-27", 4)]),  # observations
            _IterResult([]),  # classifications
            _IterResult([_MockRow("2026-07-28", 1)]),  # nodes
            _IterResult(
                [_MockRow("2026-07-28", 3), _MockRow("2026-07-27", 1)]
            ),  # edges
        ]

        response = await client.get("/v1/admin/stats/usage?days=7")
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) >= 2

        # Results are sorted newest first
        day1, day2 = body[0], body[1]
        assert day1["date"] == "2026-07-28"
        assert day1["episode_count"] == 20
        assert day1["session_count"] == 5
        assert day1["fact_count"] == 7
        assert day1["extraction_count"] == 2
        assert day1["observation_count"] == 0
        assert day1["classification_count"] == 0
        assert day1["node_count"] == 1
        assert day1["edge_count"] == 3
        assert day2["date"] == "2026-07-27"
        assert day2["episode_count"] == 15
        assert day2["session_count"] == 3
        assert day2["observation_count"] == 4
        assert day2["edge_count"] == 1
        assert mock_db.execute.await_count == 8

    async def test_uses_default_days(
        self, client: AsyncClient, mock_db: AsyncMock
    ) -> None:
        """Should default to 30 days when no days param is provided."""

        class _IterResult:
            def __init__(self, rows: list):
                self._rows = rows

            def __iter__(self):
                return iter(self._rows)

        mock_db.execute.side_effect = [
            _IterResult([]),  # episodes
            _IterResult([]),  # sessions
            _IterResult([]),  # facts
            _IterResult([]),  # extractions
            _IterResult([]),  # observations
            _IterResult([]),  # classifications
            _IterResult([]),  # nodes
            _IterResult([]),  # edges
        ]
        response = await client.get("/v1/admin/stats/usage")
        assert response.status_code == 200
        assert response.json() == []

    async def test_returns_422_on_invalid_days(
        self, client: AsyncClient, mock_db: AsyncMock
    ) -> None:
        """Should return 422 when days is out of range."""
        response = await client.get("/v1/admin/stats/usage?days=0")
        assert response.status_code == 422

        response = await client.get("/v1/admin/stats/usage?days=366")
        assert response.status_code == 422

    async def test_returns_422_on_non_integer_days(
        self, client: AsyncClient, mock_db: AsyncMock
    ) -> None:
        """Should return 422 when days is not an integer."""
        response = await client.get("/v1/admin/stats/usage?days=abc")
        assert response.status_code == 422
