"""Phase-3 contract tests — SurrealDB graph-edge expiry + effective-at traversal.

SurrealDB has no live harness; these mocked-contract tests pin the
OBSERVED SurrealQL of the Phase-3 surface:

* ``expire_relationships_matching`` — writes ``invalid_at`` guarded by
  ``invalid_at IS NONE`` (idempotent replay → 0, scenario 16 leg), with
  the deterministic ``at_time`` bound verbatim (never ``time::now()``).
* ``traverse`` — applies the effective-at edge filter when ``as_of`` is
  given (scenario 13 leg): ``(invalid_at IS NONE OR invalid_at > $as_of)``
  plus the natural ``[valid_from, valid_to]`` window.
* ``retrieve_graph`` — threads ``as_of`` into ``traverse``.
* ``list_entity_edges`` — omits expired edges via the ``[WHERE
  invalid_at IS NONE]`` edge filter in BOTH arrow branches (predicate +
  wildcard), matching Postgres/Falkor.

The ``backend`` fixture carries ``_schema_ensured = True`` so no schema
bootstrap calls hit the mocked client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

pytest.importorskip("surrealdb")

from surrealdb import RecordID
from surrealdb.errors import NotFoundError as SurrealNotFoundError

from core.exceptions import ExternalServiceError
from packages.graph_backend.surrealdb import SurrealGraphBackend

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJ_ID = UUID("00000000-0000-0000-0000-000000000002")
SRC_ID = UUID("00000000-0000-0000-0000-000000000003")
TGT_ID = UUID("00000000-0000-0000-0000-000000000004")
NEIGHBOR_ID = UUID("00000000-0000-0000-0000-000000000005")

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def mock_surreal() -> AsyncMock:
    surreal = AsyncMock()
    surreal.query.return_value = []
    return surreal


@pytest.fixture
def backend(mock_surreal: AsyncMock) -> SurrealGraphBackend:
    bk = SurrealGraphBackend(surreal=mock_surreal)
    bk._schema_ensured = True
    return bk


class TestSurrealExpireRelationshipsMatching:
    """Scenario 10b/16 (Surreal leg) — idempotent guarded expiry."""

    async def test_expires_matching_active_edges(
        self, backend: SurrealGraphBackend, mock_surreal: AsyncMock
    ) -> None:
        """Query guards ``invalid_at IS NONE`` and binds the deterministic time."""
        edge = {"id": RecordID("reports_to", "edge-1")}
        mock_surreal.query.return_value = [edge]

        count = await backend.expire_relationships_matching(
            ORG_ID,
            PROJ_ID,
            source_id=SRC_ID,
            target_id=TGT_ID,
            relationship_type="reports_to",
            at_time=T1,
        )

        assert count == 1
        query, params = mock_surreal.query.call_args.args
        assert "UPDATE reports_to" in query
        assert "SET invalid_at = $at_time" in query
        assert "invalid_at IS NONE" in query  # the double-expiry guard
        assert "RETURN BEFORE" in query
        assert params["source_id"] == RecordID("entity", str(SRC_ID))
        assert params["target_id"] == RecordID("entity", str(TGT_ID))
        # Deterministic supersession instant — never time::now() in-band.
        assert params["at_time"] == T1.isoformat()
        assert params["org_id"] == str(ORG_ID)
        assert params["project_id"] == str(PROJ_ID)

    async def test_idempotent_replay_returns_zero(
        self, backend: SurrealGraphBackend, mock_surreal: AsyncMock
    ) -> None:
        """Replay after the first expiry: ``invalid_at IS NONE`` matches none."""
        mock_surreal.query.return_value = []
        count = await backend.expire_relationships_matching(
            ORG_ID,
            PROJ_ID,
            source_id=SRC_ID,
            target_id=TGT_ID,
            relationship_type="reports_to",
            at_time=T1,
        )
        assert count == 0

    async def test_missing_edge_table_returns_zero(
        self, backend: SurrealGraphBackend, mock_surreal: AsyncMock
    ) -> None:
        """No edge table for the type has ever been created → clean 0."""
        mock_surreal.query.side_effect = SurrealNotFoundError(
            kind="table", message="no such table"
        )
        count = await backend.expire_relationships_matching(
            ORG_ID,
            PROJ_ID,
            source_id=SRC_ID,
            target_id=TGT_ID,
            relationship_type="reports_to",
            at_time=T1,
        )
        assert count == 0

    async def test_failure_raises_external_service_error(
        self, backend: SurrealGraphBackend, mock_surreal: AsyncMock
    ) -> None:
        mock_surreal.query.side_effect = RuntimeError("surreal down")
        with pytest.raises(ExternalServiceError, match="Failed to expire relationships"):
            await backend.expire_relationships_matching(
                ORG_ID,
                PROJ_ID,
                source_id=SRC_ID,
                target_id=TGT_ID,
                relationship_type="reports_to",
                at_time=T1,
            )


class TestSurrealTraverseEffectiveAt:
    """Scenario 13 (Surreal leg) — traverse honours ``as_of``."""

    def _capture(self, backend: SurrealGraphBackend) -> list[tuple[str, dict]]:
        calls: list[tuple[str, dict]] = []

        async def _side_effect(query: str, params: dict[str, Any] | None = None) -> list[Any]:
            calls.append((query, params or {}))
            return []

        backend._surreal.query.side_effect = _side_effect
        return calls

    async def test_as_of_filter_applied_when_timestamp_given(
        self, backend: SurrealGraphBackend
    ) -> None:
        """``as_of`` yields the full effective-at edge filter on neighbour fetch."""
        calls = self._capture(backend)

        await backend.traverse(ORG_ID, PROJ_ID, SRC_ID, max_depth=1, as_of=T1)

        expiry_queries = [
            q for q, _ in calls if "SELECT VALUE" in q and "->" in q
        ]
        assert expiry_queries, "neighbour-discovery query must run"
        assert "(invalid_at IS NONE OR invalid_at > $as_of)" in expiry_queries[0]
        assert "valid_from <= $as_of" in expiry_queries[0]
        assert "valid_to >= $as_of" in expiry_queries[0]
        # The concrete as_of instant is bound — SurrealDB never compares
        # against a NULL bound parameter.
        assert any(
            params.get("as_of") == T1.isoformat() for _, params in calls
        )

    async def test_superseded_edge_not_traversed_at_t2_but_is_at_t0(
        self, backend: SurrealGraphBackend, mock_surreal: AsyncMock
    ) -> None:
        """Edge invalidated at T1 is excluded at as_of=T2, included at T0.

        The neighbour-discovery query carries the as_of parameter, so the
        DB-side filter ``invalid_at > $as_of`` decides inclusion.  Asserted
        by the bound parameter + filter combination the backend emits.
        """
        calls: list[tuple[str, dict]] = []

        async def _side_effect(query: str, params: dict[str, Any] | None = None) -> list[Any]:
            calls.append((query, params or {}))
            return []

        mock_surreal.query.side_effect = _side_effect

        await backend.traverse(ORG_ID, PROJ_ID, SRC_ID, max_depth=1, as_of=T2)
        await backend.traverse(ORG_ID, PROJ_ID, SRC_ID, max_depth=1, as_of=T0)

        as_ofl = [p.get("as_of") for _, p in calls if p.get("as_of")]
        assert as_ofl == [T2.isoformat(), T0.isoformat()]

    async def test_retrieve_graph_threads_as_of_into_traverse(
        self, backend: SurrealGraphBackend, mock_surreal: AsyncMock
    ) -> None:
        """``retrieve_graph(as_of=...)`` forwards the instant to traverse."""
        # No entity match → no traverse call; match requires the search leg.
        calls: list[tuple[str, dict]] = []

        async def _side_effect(query: str, params: dict[str, Any] | None = None) -> list[Any]:
            calls.append((query, params or {}))
            if "search::score" in query:
                return [
                    {
                        "id": RecordID("entity", str(SRC_ID)),
                        "name": "Robbie",
                        "entity_type": "Person",
                        "summary": "",
                        "attributes": {},
                        "created_at": "2024-01-01T00:00:00",
                        "score": 1.0,
                    }
                ]
            if "SELECT * FROM entity" in query:
                return [
                    {
                        "id": RecordID("entity", str(SRC_ID)),
                        "name": "Robbie",
                        "entity_type": "Person",
                        "summary": "",
                        "attributes": {},
                        "created_at": "2024-01-01T00:00:00",
                    }
                ]
            return []

        mock_surreal.query.side_effect = _side_effect

        result = await backend.retrieve_graph(
            ORG_ID, PROJ_ID, query="Robbie", as_of=T2
        )

        # Distance-0 match only (no neighbours mocked) — the important
        # assertion is that the neighbour fetch carried as_of=T2.
        assert result, "matched entity must be returned at distance 0"
        neighbour_calls = [
            (q, p) for q, p in calls if "SELECT VALUE" in q and "->" in q
        ]
        assert neighbour_calls
        assert any(p.get("as_of") == T2.isoformat() for _, p in neighbour_calls)


class TestSurrealListEntityEdgesFiltersExpired:
    """Scenario 14 (Surreal leg) — expired-edge omission in list_entity_edges.

    Both arrow branches (predicate + wildcard) carry the ``[WHERE
    invalid_at IS NONE]`` edge filter, so an edge with ``invalid_at`` set
    is excluded at the query boundary — the same square-bracket edge
    filter pattern ``traverse`` uses.
    """

    async def _captured_query(
        self, backend: SurrealGraphBackend, mock_surreal: AsyncMock, **kwargs: Any
    ) -> str:
        captured: list[str] = []

        async def _side_effect(
            query: str, params: dict[str, Any] | None = None
        ) -> list[Any]:
            captured.append(query)
            return []

        mock_surreal.query.side_effect = _side_effect

        await backend.list_entity_edges(ORG_ID, PROJ_ID, SRC_ID, **kwargs)

        assert captured, "list_entity_edges must emit a query"
        return captured[0]

    async def test_predicate_branch_omits_expired_edges(
        self, backend: SurrealGraphBackend, mock_surreal: AsyncMock
    ) -> None:
        query = await self._captured_query(backend, mock_surreal, predicate="reports_to")
        assert "->reports_to[WHERE invalid_at IS NONE]" in query, (
            "predicate branch must filter invalid_at at the query boundary"
        )

    async def test_wildcard_branch_omits_expired_edges(
        self, backend: SurrealGraphBackend, mock_surreal: AsyncMock
    ) -> None:
        query = await self._captured_query(backend, mock_surreal)
        assert "<->?[WHERE invalid_at IS NONE]" in query, (
            "wildcard branch must filter invalid_at at the query boundary"
        )
