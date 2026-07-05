"""Unit tests for FalkorGraphBackend — mocked AsyncGraph.query().

These tests verify:
- Edge-type sanitisation and static helper methods in isolation.
- Entity CRUD (create, get, delete, update) — each method's Cypher
  structure, parameter binding, error wrapping, and return-value shaping.
- Relationship CRUD — MERGE pattern, type safety, error wrapping.
- Iterative BFS traversal with algo.bfs() / Cypher variable-length paths.
- BM25 full-text search (CALL db.idx.fulltext.queryNodes()).
- Paginated listing (offset-based with overflow detection).
- ``retrieve_graph`` orchestration (search → BFS → dedup → sort).
- Health check (connected, disconnected, unreachable).

Every test uses a mocked ``AsyncGraph.query()`` so no FalkorDB instance is
needed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

pytest.importorskip("falkordb")

from core.exceptions import ExternalServiceError
from packages.graph_backend.falkordb import (
    FalkorGraphBackend,
    _decode_offset_cursor,
    _encode_offset_cursor,
)

# ── Deterministic test UUIDs ─────────────────────────────────────────────────

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJ_ID = UUID("00000000-0000-0000-0000-000000000002")
ENTITY_ID = UUID("00000000-0000-0000-0000-000000000003")
NEIGHBOR_ID = UUID("00000000-0000-0000-0000-000000000004")
TARGET_ID = UUID("00000000-0000-0000-0000-000000000005")
OTHER_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000006")
REL_ID = UUID("00000000-0000-0000-0000-000000000010")

NOW_STR = "2024-01-01T00:00:00+00:00"

# ── Mock helpers ─────────────────────────────────────────────────────────────


class MockQueryResult:
    """Simulates ``falkordb.asyncio.AsyncGraph.query()`` return value."""

    def __init__(self, result_set: list[tuple[Any, ...]]) -> None:
        self.result_set = result_set
        self.stats: dict[str, Any] = {}


def make_entity_row(
    entity_id: UUID | None = None,
    name: str = "test-entity",
    entity_type: str = "Person",
    summary: str = "A test entity",
    attributes: str = "{}",
    created_at: str = NOW_STR,
) -> tuple[Any, ...]:
    """Build a tuple that mimics a FalkorDB entity result row.

    Column order: id, name, entity_type, summary, attributes, created_at.
    """
    eid = entity_id or ENTITY_ID
    return (str(eid), name, entity_type, summary, attributes, created_at)


def make_relationship_row(
    rel_id: UUID | None = None,
    source_id: UUID | None = None,
    target_id: UUID | None = None,
    rel_type: str = "likes",
    properties: str = "{}",
    fact: str = "",
    confidence: float = 1.0,
    valid_from: str | None = None,
    valid_to: str | None = None,
    created_at: str = NOW_STR,
) -> tuple[Any, ...]:
    """Build a tuple that mimics a FalkorDB relationship result row.

    Column order: id, source_id, target_id, type, properties, fact,
    confidence, valid_from, valid_to, created_at.
    """
    rid = rel_id or REL_ID
    sid = source_id or ENTITY_ID
    tid = target_id or TARGET_ID
    return (
        str(rid),
        str(sid),
        str(tid),
        rel_type,
        properties,
        fact,
        confidence,
        valid_from,
        valid_to,
        created_at,
    )


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_client() -> MagicMock:
    """A mocked ``FalkorDB`` client.

    MUST be ``MagicMock`` (not ``AsyncMock``) because ``select_graph()``
    is a **sync** local operation — ``AsyncMock`` would turn it into a
    coroutine and break ``_get_graph()``.
    """
    client = MagicMock()
    # The graph returned by select_graph must have `query` as a pre-assigned
    # AsyncMock (not auto-created via __getattr__) because each __getattr__
    # access would create a different AsyncMock instance, breaking
    # return_value and side_effect setup.
    graph_mock = AsyncMock()
    graph_mock.query = AsyncMock()
    client.select_graph.return_value = graph_mock
    return client


@pytest.fixture
def mock_graph(mock_client: MagicMock) -> AsyncMock:
    """The ``AsyncGraph`` instance returned by ``select_graph()``."""
    return mock_client.select_graph.return_value


@pytest.fixture
def backend(mock_client: MagicMock) -> FalkorGraphBackend:
    """A ``FalkorGraphBackend`` with schema bootstrap pre-completed."""
    bk = FalkorGraphBackend(client=mock_client, max_traversal_depth=2)
    bk._schema_ensured = {"openzync_00000000-0000-0000-0000-000000000001_00000000-0000-0000-0000-000000000002": True}
    return bk


@pytest.fixture
def setup_traverse(mock_graph: AsyncMock) -> Callable[..., None]:
    """Configure ``mock_graph.query`` for BFS-traversal tests.

    The side-effect function inspects each query call to distinguish
    entity-fetch (RETURN n.id, n.name, …) from neighbour discovery
    (RETURN DISTINCT neighbour.id) and dispatches accordingly.
    """
    _entities: dict[str, tuple[Any, ...]] = {}
    _neighbors: dict[str, list[str]] = {}

    def configure(
        *,
        entities: dict[str, tuple[Any, ...]],
        neighbors: dict[str, list[str]],
    ) -> None:
        _entities.clear()
        _entities.update(entities)
        _neighbors.clear()
        _neighbors.update(neighbors)

        async def side_effect(query: str, params: dict[str, Any] | None = None) -> MockQueryResult:
            if not params:
                return MockQueryResult([])

            # Neighbour discovery — RETURN DISTINCT neighbour.id
            if "RETURN DISTINCT neighbour.id" in query or "RETURN DISTINCT node.id" in query:
                cid = params.get("eid", "")
                nids = _neighbors.get(str(cid), [])
                return MockQueryResult([(nid,) for nid in nids])

            # Entity fetch — RETURN n.id, n.name, ...
            if "RETURN n.id, n.name" in query:
                eid = params.get("id", "")
                rec = _entities.get(str(eid))
                if rec is not None:
                    return MockQueryResult([rec])
                # Try matching by $eid as well
                eid2 = params.get("eid", "")
                rec2 = _entities.get(str(eid2))
                if rec2 is not None:
                    return MockQueryResult([rec2])
                return MockQueryResult([])

            return MockQueryResult([])

        mock_graph.query.side_effect = side_effect

    return configure


# ── Test Cases ───────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestFalkorGraphBackendSanitize:
    """``_sanitize_edge_type`` validation."""

    @staticmethod
    def test_sanitize_edge_type_valid() -> None:
        """Valid names: alphanumeric + underscores only."""
        assert FalkorGraphBackend._sanitize_edge_type("likes") == "likes"
        assert FalkorGraphBackend._sanitize_edge_type("has_role_123") == "has_role_123"

    @staticmethod
    def test_sanitize_edge_type_invalid() -> None:
        """Invalid names: spaces, dashes, leading digits, empty."""
        for bad in ("two words", "dash-ed", "123abc", ""):
            with pytest.raises(ValueError, match="Unsafe edge type"):
                FalkorGraphBackend._sanitize_edge_type(bad)

    @staticmethod
    def test_sanitize_edge_type_sql_injection() -> None:
        """Known SQL-like patterns are rejected."""
        for bad in ("DROP TABLE", "1; SELECT *", "'; DELETE"):
            with pytest.raises(ValueError, match="Unsafe edge type"):
                FalkorGraphBackend._sanitize_edge_type(bad)


@pytest.mark.unit
class TestFalkorGraphBackendHelpers:
    """Static / class helper methods and cursor utilities."""

    @staticmethod
    def test_cursor_roundtrip() -> None:
        """Base64 offset cursor encode → decode is lossless."""
        for offset in (0, 1, 5, 9999):
            encoded = _encode_offset_cursor(offset)
            decoded = _decode_offset_cursor(encoded)
            assert decoded == offset

        # None / empty → 0
        assert _decode_offset_cursor(None) == 0
        assert _decode_offset_cursor("") == 0

        # Invalid base64 → 0 (graceful)
        assert _decode_offset_cursor("!!!invalid!!!") == 0

    @staticmethod
    def test_parse_json_field() -> None:
        """_parse_json_field handles dict, string, None, and invalid."""
        assert FalkorGraphBackend._parse_json_field({"a": 1}) == {"a": 1}
        assert FalkorGraphBackend._parse_json_field('{"b": 2}') == {"b": 2}
        assert FalkorGraphBackend._parse_json_field(None) == {}
        assert FalkorGraphBackend._parse_json_field("") == {}
        assert FalkorGraphBackend._parse_json_field("not-json") == {}

    @staticmethod
    def test_row_to_entity() -> None:
        """_row_to_entity converts a 6-column tuple to an entity dict."""
        row = ("eid", "test", "Person", "desc", '{"k":"v"}', NOW_STR)
        entity = FalkorGraphBackend._row_to_entity(row)
        assert entity["id"] == "eid"
        assert entity["name"] == "test"
        assert entity["type"] == "Person"
        assert entity["summary"] == "desc"
        assert entity["attributes"] == {"k": "v"}
        assert entity["created_at"] == NOW_STR

    @staticmethod
    def test_row_to_relationship() -> None:
        """_row_to_relationship converts a 10-column tuple to a rel dict."""
        row = ("rid", "src", "tgt", "knows", '{"w":3}', "fact_str", 0.95, NOW_STR, None, NOW_STR)
        rel = FalkorGraphBackend._row_to_relationship(row)
        assert rel["id"] == "rid"
        assert rel["source_id"] == "src"
        assert rel["target_id"] == "tgt"
        assert rel["type"] == "knows"
        assert rel["properties"] == {"w": 3}
        assert rel["fact"] == "fact_str"
        assert rel["confidence"] == 0.95
        assert rel["valid_from"] == NOW_STR
        assert rel["valid_to"] is None

    @staticmethod
    def test_graph_key_isolation() -> None:
        """_get_graph produces isolated graph keys per org+project."""
        client = MagicMock()
        # Return distinct mocks per call so we can distinguish them
        client.select_graph.side_effect = lambda key: f"graph:{key}"
        bk = FalkorGraphBackend(client=client)
        g1 = bk._get_graph(UUID("11111111-1111-1111-1111-111111111111"), UUID("22222222-2222-2222-2222-222222222222"))
        g2 = bk._get_graph(UUID("33333333-3333-3333-3333-333333333333"), UUID("44444444-4444-4444-4444-444444444444"))

        assert client.select_graph.call_args_list[0][0][0] == "openzync_11111111-1111-1111-1111-111111111111_22222222-2222-2222-2222-222222222222"
        assert client.select_graph.call_args_list[1][0][0] == "openzync_33333333-3333-3333-3333-333333333333_44444444-4444-4444-4444-444444444444"
        assert g1 is not g2


@pytest.mark.unit
class TestFalkorGraphBackendEntityCrud:
    """Entity create, get, delete, update — Cypher structure + return shape."""

    # ── create_entity ─────────────────────────────────────────────────────

    @staticmethod
    async def test_create_entity(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """New entity: verifies MERGE pattern and return shape."""
        result_row = make_entity_row(name="test", entity_type="Person")
        mock_graph.query.return_value = MockQueryResult([result_row])

        result = await backend.create_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            name="Test",
            entity_type="Person",
        )

        # Cypher contains MERGE
        call = mock_graph.query.call_args
        query = call[0][0]
        assert "MERGE (n:Entity {name: $name})" in query
        assert "ON CREATE SET" in query
        assert "ON MATCH SET" in query

        # Params: name is lowercased
        params = call[0][1]
        assert params["name"] == "test"
        assert params["type"] == "Person"

        # Return shape
        assert result["id"] is not None
        assert result["name"] == "test"
        assert result["type"] == "Person"

    @staticmethod
    async def test_create_entity_name_normalized(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Entity name is lowered and stripped before query."""
        result_row = make_entity_row(name="mixedcase", entity_type="Custom")
        mock_graph.query.return_value = MockQueryResult([result_row])

        await backend.create_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            name="  MixedCase  ",
            entity_type="Custom",
        )

        params = mock_graph.query.call_args[0][1]
        assert params["name"] == "mixedcase"

    @staticmethod
    async def test_create_entity_db_error(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """FalkorDB error is wrapped in ``ExternalServiceError``."""
        mock_graph.query.side_effect = RuntimeError("DB connection lost")

        with pytest.raises(ExternalServiceError, match="DB connection lost"):
            await backend.create_entity(
                org_id=ORG_ID,
                project_id=PROJ_ID,
                name="Test",
                entity_type="Person",
            )

    @staticmethod
    async def test_create_entity_no_client() -> None:
        """No client → raises ExternalServiceError."""
        bk = FalkorGraphBackend(client=None)
        with pytest.raises(ExternalServiceError, match="FalkorDB not connected"):
            await bk.create_entity(
                org_id=ORG_ID,
                project_id=PROJ_ID,
                name="Test",
                entity_type="Person",
            )

    # ── get_entity ────────────────────────────────────────────────────────

    @staticmethod
    @pytest.mark.parametrize(
        ("scenario", "query_return", "expected_name"),
        [
            ("found", MockQueryResult([make_entity_row()]), "test-entity"),
            ("not_found", MockQueryResult([]), None),
        ],
    )
    async def test_get_entity(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
        scenario: str,
        query_return: MockQueryResult,
        expected_name: str | None,
    ) -> None:
        """Entity retrieval returns dict or None."""
        mock_graph.query.return_value = query_return

        result = await backend.get_entity(ORG_ID, PROJ_ID, ENTITY_ID)

        if expected_name is None:
            assert result is None
        else:
            assert result is not None
            assert result["name"] == expected_name
            assert result["id"] == str(ENTITY_ID)

    @staticmethod
    async def test_get_entity_no_client() -> None:
        """No client → returns None (graceful)."""
        bk = FalkorGraphBackend(client=None)
        result = await bk.get_entity(ORG_ID, PROJ_ID, ENTITY_ID)
        assert result is None

    @staticmethod
    async def test_get_entity_db_error(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """FalkorDB error is wrapped in ``ExternalServiceError``."""
        mock_graph.query.side_effect = RuntimeError("query timeout")
        with pytest.raises(ExternalServiceError, match="query timeout"):
            await backend.get_entity(ORG_ID, PROJ_ID, ENTITY_ID)

    # ── delete_entity ─────────────────────────────────────────────────────

    @staticmethod
    @pytest.mark.parametrize(
        ("scenario", "query_return", "expected"),
        [
            ("exists", MockQueryResult([(1,)]), True),
            ("not_exists", MockQueryResult([(0,)]), False),
        ],
    )
    async def test_delete_entity(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
        scenario: str,
        query_return: MockQueryResult,
        expected: bool,
    ) -> None:
        """Delete returns True or False based on RETURN count."""
        mock_graph.query.return_value = query_return

        result = await backend.delete_entity(ORG_ID, PROJ_ID, ENTITY_ID)
        assert result is expected

    @staticmethod
    async def test_delete_entity_no_client() -> None:
        """No client → returns False (graceful)."""
        bk = FalkorGraphBackend(client=None)
        result = await bk.delete_entity(ORG_ID, PROJ_ID, ENTITY_ID)
        assert result is False

    @staticmethod
    async def test_delete_entity_db_error(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """FalkorDB error is wrapped in ``ExternalServiceError``."""
        mock_graph.query.side_effect = RuntimeError("delete failed")
        with pytest.raises(ExternalServiceError, match="delete failed"):
            await backend.delete_entity(ORG_ID, PROJ_ID, ENTITY_ID)

    # ── update_entity ─────────────────────────────────────────────────────

    @staticmethod
    async def test_update_entity_partial(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Only provided fields are included in the SET clause."""
        result_row = make_entity_row(name="updated-name")
        mock_graph.query.return_value = MockQueryResult([result_row])

        result = await backend.update_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
            name="Updated-Name",
        )

        query = mock_graph.query.call_args[0][0]
        params = mock_graph.query.call_args[0][1]
        assert "n.name = $name" in query
        assert "n.summary = $summary" not in query  # not provided
        assert "n.entity_type = $entity_type" not in query
        assert "n.attributes = $attributes" not in query
        assert "n.updated_at = $now" in query
        assert "now" in params
        assert params["now"] != ""

        assert result is not None
        assert result["name"] == "updated-name"

    @staticmethod
    async def test_update_entity_full(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """All fields are included when provided."""
        result_row = make_entity_row(name="new-name", entity_type="Org", summary="new summary")
        mock_graph.query.return_value = MockQueryResult([result_row])

        await backend.update_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
            name="New-Name",
            summary="new summary",
            entity_type="Org",
            attributes={"key": "val"},
        )

        query = mock_graph.query.call_args[0][0]
        params = mock_graph.query.call_args[0][1]
        assert "n.name = $name" in query
        assert "n.summary = $summary" in query
        assert "n.entity_type = $entity_type" in query
        assert "n.attributes = $attributes" in query
        assert "n.updated_at = $now" in query
        assert "now" in params
        assert params["now"] != ""

    @staticmethod
    async def test_update_entity_not_found(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Returns None when entity does not exist."""
        mock_graph.query.return_value = MockQueryResult([])

        result = await backend.update_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
            name="Anything",
        )
        assert result is None

    @staticmethod
    async def test_update_entity_no_fields(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """No fields to update → falls through to ``get_entity``."""
        mock_graph.query.return_value = MockQueryResult([make_entity_row()])

        result = await backend.update_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
        )

        # Should have called query twice (once for no-op fallback get_entity path)
        assert mock_graph.query.called
        # The actual query should be a MATCH with SET (via get_entity)
        # When no fields are set, update_entity calls get_entity which runs MATCH
        assert result is not None
        assert result["id"] == str(ENTITY_ID)

    @staticmethod
    async def test_update_entity_no_client() -> None:
        """No client → returns None (graceful)."""
        bk = FalkorGraphBackend(client=None)
        result = await bk.update_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
            name="Anything",
        )
        assert result is None

    @staticmethod
    async def test_update_entity_db_error(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """FalkorDB error is wrapped in ``ExternalServiceError``."""
        mock_graph.query.side_effect = RuntimeError("update failed")
        with pytest.raises(ExternalServiceError, match="update failed"):
            await backend.update_entity(
                org_id=ORG_ID,
                project_id=PROJ_ID,
                entity_id=ENTITY_ID,
                name="Anything",
            )


@pytest.mark.unit
class TestFalkorGraphBackendRelationships:
    """Relationship create with MERGE pattern."""

    @staticmethod
    async def test_create_relationship(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """New relationship creates via MERGE."""
        result_row = make_relationship_row(
            source_id=ENTITY_ID,
            target_id=TARGET_ID,
            rel_type="likes",
        )
        mock_graph.query.return_value = MockQueryResult([result_row])

        result = await backend.create_relationship(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            source_id=ENTITY_ID,
            target_id=TARGET_ID,
            relationship_type="likes",
        )

        query = mock_graph.query.call_args[0][0]
        assert "MERGE (s:Entity" in query
        assert "-[r:likes]->" in query
        assert result["source_id"] == str(ENTITY_ID)
        assert result["target_id"] == str(TARGET_ID)
        assert result["type"] == "likes"

    @staticmethod
    async def test_create_relationship_invalid_type(
        backend: FalkorGraphBackend,
    ) -> None:
        """Invalid edge type name raises ``ValueError``."""
        with pytest.raises(ValueError, match="Unsafe edge type"):
            await backend.create_relationship(
                org_id=ORG_ID,
                project_id=PROJ_ID,
                source_id=ENTITY_ID,
                target_id=TARGET_ID,
                relationship_type="bad type with spaces",
            )

    @staticmethod
    async def test_create_relationship_no_client() -> None:
        """No client → raises ExternalServiceError."""
        bk = FalkorGraphBackend(client=None)
        with pytest.raises(ExternalServiceError, match="FalkorDB not connected"):
            await bk.create_relationship(
                org_id=ORG_ID,
                project_id=PROJ_ID,
                source_id=ENTITY_ID,
                target_id=TARGET_ID,
                relationship_type="likes",
            )

    @staticmethod
    async def test_create_relationship_error(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """FalkorDB error is wrapped in ``ExternalServiceError``."""
        mock_graph.query.side_effect = RuntimeError("falkordb timeout")
        with pytest.raises(ExternalServiceError, match="falkordb timeout"):
            await backend.create_relationship(
                org_id=ORG_ID,
                project_id=PROJ_ID,
                source_id=ENTITY_ID,
                target_id=TARGET_ID,
                relationship_type="likes",
            )

    # ── get_relationships ──────────────────────────────────────────────────

    @staticmethod
    async def test_get_relationships(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Returns list of relationships for an entity."""
        result_rows = [
            make_relationship_row(
                rel_id=REL_ID, source_id=ENTITY_ID, target_id=TARGET_ID, rel_type="likes",
            ),
        ]
        mock_graph.query.return_value = MockQueryResult(result_rows)

        result = await backend.get_relationships(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
        )

        assert len(result) == 1
        assert result[0]["source_id"] == str(ENTITY_ID)
        assert result[0]["target_id"] == str(TARGET_ID)
        assert result[0]["type"] == "likes"

    @staticmethod
    async def test_get_relationships_with_type_filter(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Type filter is included in the Cypher WHERE clause."""
        mock_graph.query.return_value = MockQueryResult([])

        await backend.get_relationships(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
            relationship_type="knows",
        )

        query = mock_graph.query.call_args[0][0]
        assert "type(r) = 'knows'" in query

    @staticmethod
    async def test_get_relationships_no_client() -> None:
        """No client → returns empty list (graceful)."""
        bk = FalkorGraphBackend(client=None)
        result = await bk.get_relationships(ORG_ID, PROJ_ID, ENTITY_ID)
        assert result == []

    @staticmethod
    async def test_get_relationships_error(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """FalkorDB error is wrapped in ``ExternalServiceError``."""
        mock_graph.query.side_effect = RuntimeError("query failed")
        with pytest.raises(ExternalServiceError, match="query failed"):
            await backend.get_relationships(ORG_ID, PROJ_ID, ENTITY_ID)

    # ── expire_relationship ────────────────────────────────────────────────

    @staticmethod
    @pytest.mark.parametrize(
        ("scenario", "query_return", "expected"),
        [
            ("found", MockQueryResult([(1,)]), True),
            ("not_found", MockQueryResult([(0,)]), False),
        ],
    )
    async def test_expire_relationship(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
        scenario: str,
        query_return: MockQueryResult,
        expected: bool,
    ) -> None:
        """Soft-delete sets invalid_at and returns True/False."""
        mock_graph.query.return_value = query_return

        result = await backend.expire_relationship(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            relationship_id=REL_ID,
        )
        assert result is expected

    @staticmethod
    async def test_expire_relationship_no_client() -> None:
        """No client → returns False (graceful)."""
        bk = FalkorGraphBackend(client=None)
        result = await bk.expire_relationship(ORG_ID, PROJ_ID, REL_ID)
        assert result is False

    @staticmethod
    async def test_expire_relationship_error(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """FalkorDB error is wrapped in ``ExternalServiceError``."""
        mock_graph.query.side_effect = RuntimeError("expire failed")
        with pytest.raises(ExternalServiceError, match="expire failed"):
            await backend.expire_relationship(ORG_ID, PROJ_ID, REL_ID)


@pytest.mark.unit
class TestFalkorGraphBackendTraversal:
    """BFS traversal with algo.bfs() and Cypher variable-length paths.

    Uses the ``setup_traverse`` fixture — a side-effect function on
    ``mock_graph.query`` that dispatches to entity records or neighbour
    ID lists based on the query text.
    """

    @staticmethod
    async def test_traverse_single_hop(
        backend: FalkorGraphBackend,
        setup_traverse: Callable[..., None],
    ) -> None:
        """Start node + one neighbour are returned at depths 0 and 1."""
        setup_traverse(
            entities={
                str(ENTITY_ID): make_entity_row(entity_id=ENTITY_ID, name="Start"),
                str(NEIGHBOR_ID): make_entity_row(entity_id=NEIGHBOR_ID, name="Neighbor"),
            },
            neighbors={str(ENTITY_ID): [str(NEIGHBOR_ID)]},
        )

        result = await backend.traverse(ORG_ID, PROJ_ID, ENTITY_ID, max_depth=2)

        assert len(result) == 2
        assert result[0]["id"] == str(ENTITY_ID)
        assert result[0]["depth"] == 0
        assert result[1]["id"] == str(NEIGHBOR_ID)
        assert result[1]["depth"] == 1

    @staticmethod
    async def test_traverse_multi_hop(
        backend: FalkorGraphBackend,
        setup_traverse: Callable[..., None],
    ) -> None:
        """Multi-hop traversal reaches depth 2."""
        third_id = UUID("00000000-0000-0000-0000-000000000007")
        setup_traverse(
            entities={
                str(ENTITY_ID): make_entity_row(entity_id=ENTITY_ID, name="Start"),
                str(NEIGHBOR_ID): make_entity_row(entity_id=NEIGHBOR_ID, name="Hop1"),
                str(third_id): make_entity_row(entity_id=third_id, name="Hop2"),
            },
            neighbors={
                str(ENTITY_ID): [str(NEIGHBOR_ID)],
                str(NEIGHBOR_ID): [str(third_id)],
            },
        )

        result = await backend.traverse(ORG_ID, PROJ_ID, ENTITY_ID, max_depth=5)

        assert len(result) == 3
        assert result[0]["depth"] == 0
        assert result[1]["depth"] == 1
        assert result[2]["depth"] == 2

    @staticmethod
    async def test_traverse_with_single_edge_type(
        backend: FalkorGraphBackend,
        setup_traverse: Callable[..., None],
        mock_graph: AsyncMock,
    ) -> None:
        """Single edge type: uses algo.bfs() path."""
        setup_traverse(
            entities={
                str(ENTITY_ID): make_entity_row(entity_id=ENTITY_ID, name="Start"),
                str(NEIGHBOR_ID): make_entity_row(entity_id=NEIGHBOR_ID, name="Neighbor"),
            },
            neighbors={str(ENTITY_ID): [str(NEIGHBOR_ID)]},
        )

        await backend.traverse(
            ORG_ID,
            PROJ_ID,
            ENTITY_ID,
            max_depth=2,
            edge_types=["likes"],
        )

        query = mock_graph.query.call_args_list[1][0][0]  # second query = neighbor discovery
        assert "algo.bfs" in query
        assert "likes" in query

    @staticmethod
    async def test_traverse_with_multi_edge_types(
        backend: FalkorGraphBackend,
        setup_traverse: Callable[..., None],
        mock_graph: AsyncMock,
    ) -> None:
        """Multiple edge types: uses Cypher variable-length path."""
        setup_traverse(
            entities={
                str(ENTITY_ID): make_entity_row(entity_id=ENTITY_ID, name="Start"),
                str(NEIGHBOR_ID): make_entity_row(entity_id=NEIGHBOR_ID, name="Neighbor"),
            },
            neighbors={str(ENTITY_ID): [str(NEIGHBOR_ID)]},
        )

        await backend.traverse(
            ORG_ID,
            PROJ_ID,
            ENTITY_ID,
            max_depth=2,
            edge_types=["likes", "knows"],
        )

        query = mock_graph.query.call_args_list[1][0][0]
        assert "likes|knows" in query
        assert "algo.bfs" not in query  # multi-type does not use algo.bfs

    @staticmethod
    async def test_traverse_all_types(
        backend: FalkorGraphBackend,
        setup_traverse: Callable[..., None],
        mock_graph: AsyncMock,
    ) -> None:
        """No edge_types: uses wildcard [r]."""
        setup_traverse(
            entities={
                str(ENTITY_ID): make_entity_row(entity_id=ENTITY_ID, name="Start"),
                str(NEIGHBOR_ID): make_entity_row(entity_id=NEIGHBOR_ID, name="Neighbor"),
            },
            neighbors={str(ENTITY_ID): [str(NEIGHBOR_ID)]},
        )

        await backend.traverse(
            ORG_ID,
            PROJ_ID,
            ENTITY_ID,
            max_depth=2,
            edge_types=None,  # all types
        )

        query = mock_graph.query.call_args_list[1][0][0]
        assert "[r]" in query
        assert "algo.bfs" not in query

    @staticmethod
    async def test_traverse_empty_edge_types(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Empty edge_types list returns just the start node (no BFS)."""
        mock_graph.query.return_value = MockQueryResult([make_entity_row()])

        result = await backend.traverse(
            ORG_ID,
            PROJ_ID,
            ENTITY_ID,
            max_depth=2,
            edge_types=[],
        )

        assert len(result) == 1
        assert result[0]["id"] == str(ENTITY_ID)
        assert result[0]["depth"] == 0

    @staticmethod
    async def test_traverse_respects_max_depth(
        backend: FalkorGraphBackend,
        setup_traverse: Callable[..., None],
    ) -> None:
        """BFS does not go beyond ``max_depth``."""
        setup_traverse(
            entities={
                str(ENTITY_ID): make_entity_row(entity_id=ENTITY_ID, name="Start"),
                str(NEIGHBOR_ID): make_entity_row(entity_id=NEIGHBOR_ID, name="Neighbor"),
            },
            neighbors={str(ENTITY_ID): [str(NEIGHBOR_ID)]},
        )

        result = await backend.traverse(
            ORG_ID,
            PROJ_ID,
            ENTITY_ID,
            max_depth=0,  # only start node
        )

        assert len(result) == 1
        assert result[0]["depth"] == 0

    @staticmethod
    async def test_traverse_start_not_found(
        backend: FalkorGraphBackend,
        setup_traverse: Callable[..., None],
    ) -> None:
        """Start node not found → empty list."""
        setup_traverse(entities={}, neighbors={})

        result = await backend.traverse(ORG_ID, PROJ_ID, ENTITY_ID, max_depth=2)
        assert result == []

    @staticmethod
    async def test_traverse_no_client() -> None:
        """No client → returns empty list (graceful)."""
        bk = FalkorGraphBackend(client=None)
        result = await bk.traverse(ORG_ID, PROJ_ID, ENTITY_ID)
        assert result == []


@pytest.mark.unit
class TestFalkorGraphBackendSearchAndListing:
    """BM25 full-text search and paginated entity/edge listing."""

    # ── search_entities ───────────────────────────────────────────────────

    @staticmethod
    async def test_search_entities(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Search uses ``CALL db.idx.fulltext.queryNodes()``."""
        result_row = make_entity_row(name="findable", summary="something") + (0.85,)
        mock_graph.query.return_value = MockQueryResult([result_row])

        result = await backend.search_entities(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            query="findable",
        )

        query = mock_graph.query.call_args[0][0]
        assert "db.idx.fulltext.queryNodes" in query
        assert "score" in query
        assert len(result) == 1
        assert result[0]["score"] == 0.85

    @staticmethod
    async def test_search_entities_no_results(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """No matches → empty list."""
        mock_graph.query.return_value = MockQueryResult([])

        result = await backend.search_entities(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            query="nothing",
        )
        assert result == []

    @staticmethod
    async def test_search_entities_with_types(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Type filter is passed as parameter."""
        result_row = make_entity_row(name="findable", entity_type="Person") + (0.9,)
        mock_graph.query.return_value = MockQueryResult([result_row])

        result = await backend.search_entities(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            query="findable",
            types=["Person"],
        )

        assert len(result) == 1
        assert result[0]["type"] == "Person"

    @staticmethod
    async def test_search_entities_no_client() -> None:
        """No client → returns empty list (graceful)."""
        bk = FalkorGraphBackend(client=None)
        result = await bk.search_entities(ORG_ID, PROJ_ID, "test")
        assert result == []

    @staticmethod
    async def test_search_entities_error(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """FalkorDB error is wrapped in ``ExternalServiceError``."""
        mock_graph.query.side_effect = RuntimeError("search failed")
        with pytest.raises(ExternalServiceError, match="search failed"):
            await backend.search_entities(ORG_ID, PROJ_ID, "test")

    # ── list_entities ─────────────────────────────────────────────────────

    @staticmethod
    async def test_list_entities_no_pagination(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Fewer items than limit → no overflow, no cursor."""
        rows = [
            make_entity_row(entity_id=ENTITY_ID, name="A"),
            make_entity_row(entity_id=NEIGHBOR_ID, name="B"),
        ]
        mock_graph.query.return_value = MockQueryResult(rows)  # 2 items, limit=3 → no overflow

        result = await backend.list_entities(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            limit=3,
        )

        assert len(result["items"]) == 2
        assert result["has_more"] is False
        assert result["next_cursor"] is None

    @staticmethod
    async def test_list_entities_with_overflow(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """More items than limit → overflow detected, cursor returned."""
        rows = [
            make_entity_row(
                entity_id=UUID(f"00000000-0000-0000-0000-{i:012d}"),
                name=f"E{i}",
            )
            for i in range(6)
        ]
        mock_graph.query.return_value = MockQueryResult(rows)  # 6 items, limit=5 → overflow

        result = await backend.list_entities(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            limit=5,
        )

        assert len(result["items"]) == 5
        assert result["has_more"] is True
        assert result["next_cursor"] is not None

    @staticmethod
    async def test_list_entities_with_type_filter(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Type filter is included in the Cypher WHERE clause."""
        mock_graph.query.return_value = MockQueryResult([])

        await backend.list_entities(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_type="Person",
            limit=10,
        )

        query = mock_graph.query.call_args[0][0]
        assert "n.entity_type = $entity_type" in query

    @staticmethod
    async def test_list_entities_no_client() -> None:
        """No client → returns empty result (graceful)."""
        bk = FalkorGraphBackend(client=None)
        result = await bk.list_entities(ORG_ID, PROJ_ID)
        assert result == {"items": [], "next_cursor": None, "has_more": False}

    @staticmethod
    async def test_list_entities_error(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """FalkorDB error is wrapped in ``ExternalServiceError``."""
        mock_graph.query.side_effect = RuntimeError("list failed")
        with pytest.raises(ExternalServiceError, match="list failed"):
            await backend.list_entities(ORG_ID, PROJ_ID)

    # ── list_entity_edges ─────────────────────────────────────────────────

    @staticmethod
    async def test_list_entity_edges(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Returns paginated edges for an entity."""
        rows = [
            make_relationship_row(
                rel_id=REL_ID, source_id=ENTITY_ID, target_id=TARGET_ID, rel_type="likes",
            ),
        ]
        mock_graph.query.return_value = MockQueryResult(rows)

        result = await backend.list_entity_edges(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
        )

        assert len(result["items"]) == 1
        assert result["items"][0]["source_id"] == str(ENTITY_ID)
        assert result["items"][0]["target_id"] == str(TARGET_ID)

    @staticmethod
    async def test_list_entity_edges_with_predicate(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """With predicate: type filter in Cypher."""
        mock_graph.query.return_value = MockQueryResult([])

        await backend.list_entity_edges(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
            predicate="knows",
        )

        query = mock_graph.query.call_args[0][0]
        assert "type(r) = 'knows'" in query

    @staticmethod
    async def test_list_entity_edges_invalid_predicate(
        backend: FalkorGraphBackend,
    ) -> None:
        """Invalid predicate type raises ``ValueError``."""
        with pytest.raises(ValueError, match="Unsafe edge type"):
            await backend.list_entity_edges(
                org_id=ORG_ID,
                project_id=PROJ_ID,
                entity_id=ENTITY_ID,
                predicate="bad pred",
            )

    @staticmethod
    async def test_list_entity_edges_no_client() -> None:
        """No client → returns empty result (graceful)."""
        bk = FalkorGraphBackend(client=None)
        result = await bk.list_entity_edges(ORG_ID, PROJ_ID, ENTITY_ID)
        assert result == {"items": [], "next_cursor": None, "has_more": False}

    @staticmethod
    async def test_list_entity_edges_error(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """FalkorDB error is wrapped in ``ExternalServiceError``."""
        mock_graph.query.side_effect = RuntimeError("edges failed")
        with pytest.raises(ExternalServiceError, match="edges failed"):
            await backend.list_entity_edges(ORG_ID, PROJ_ID, ENTITY_ID)

    # ── get_entity_with_edges ─────────────────────────────────────────────

    @staticmethod
    async def test_get_entity_with_edges(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Returns entity node + edges."""
        entity_row = make_entity_row()
        edge_row = make_relationship_row(
            rel_id=REL_ID, source_id=ENTITY_ID, target_id=TARGET_ID,
        )

        # First call = get_entity, second = list_entity_edges
        mock_graph.query.side_effect = [
            MockQueryResult([entity_row]),
            MockQueryResult([edge_row]),
        ]

        result = await backend.get_entity_with_edges(ORG_ID, PROJ_ID, ENTITY_ID)

        assert result is not None
        assert result["node"]["id"] == str(ENTITY_ID)
        assert len(result["edges"]) == 1
        assert result["edges"][0]["source_id"] == str(ENTITY_ID)

    @staticmethod
    async def test_get_entity_with_edges_not_found(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Entity not found → returns None."""
        mock_graph.query.return_value = MockQueryResult([])

        result = await backend.get_entity_with_edges(ORG_ID, PROJ_ID, ENTITY_ID)
        assert result is None


@pytest.mark.unit
class TestFalkorGraphBackendRetrieveGraph:
    """``retrieve_graph`` — search → BFS → dedup → sort by distance."""

    @staticmethod
    async def test_retrieve_graph(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """Matches search, then BFS-traverses outward, deduplicates, sorts."""
        # Mock search_entities to return 1 match
        match_row = make_entity_row(name="Match", entity_type="Person")
        mock_graph.query.return_value = MockQueryResult([match_row])

        # We need to control the second set of calls (traverse neighbour discovery)
        # For simplicity, mock get_entity inside traverse
        entity_row = make_entity_row(entity_id=ENTITY_ID, name="Match")
        neighbor_row = make_entity_row(entity_id=NEIGHBOR_ID, name="Neighbor")
        mock_graph.query.side_effect = [
            MockQueryResult([match_row]),  # search_entities
            MockQueryResult([entity_row]),  # get_entity for start node in traverse
            MockQueryResult([]),           # traverse: no more neighbors after entity
        ]

        result = await backend.retrieve_graph(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            query="find",
            match_limit=5,
            max_depth=2,
            max_results=50,
        )

        # Should have the matched entity
        assert len(result) >= 1
        assert result[0]["id"] == str(ENTITY_ID)
        assert result[0]["distance"] == 0

    @staticmethod
    async def test_retrieve_graph_no_matches(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """No search matches → empty list."""
        mock_graph.query.return_value = MockQueryResult([])

        result = await backend.retrieve_graph(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            query="nothing",
        )
        assert result == []

    @staticmethod
    async def test_retrieve_graph_no_client() -> None:
        """No client → returns empty list (graceful)."""
        bk = FalkorGraphBackend(client=None)
        result = await bk.retrieve_graph(ORG_ID, PROJ_ID, "test")
        assert result == []


@pytest.mark.unit
class TestFalkorGraphBackendHealthCheck:
    """Health-check scenarios."""

    @staticmethod
    async def test_health_check_ok(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """FalkorDB reachable → ``True``."""
        mock_graph.query.return_value = MockQueryResult([(1,)])
        assert await backend.health_check() is True

    @staticmethod
    async def test_health_check_fail(
        backend: FalkorGraphBackend,
        mock_graph: AsyncMock,
    ) -> None:
        """FalkorDB unreachable → ``False``."""
        mock_graph.query.side_effect = RuntimeError("connection refused")
        assert await backend.health_check() is False

    @staticmethod
    async def test_health_check_not_connected() -> None:
        """No FalkorDB connection → ``False``."""
        bk = FalkorGraphBackend(client=None)
        assert await bk.health_check() is False
