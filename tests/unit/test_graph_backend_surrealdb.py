"""Unit tests for SurrealGraphBackend — mocked AsyncSurreal.

These tests verify:
- Edge-type sanitisation and helper methods in isolation.
- Entity CRUD (create, get, delete, update) — each method's SurrealQL
  structure, parameter binding, error wrapping, and return-value shaping.
- Relationship CRUD — RELATE vs UPDATE-on-existing branching, type safety,
  error wrapping.
- BFS traversal with native arrow syntax (``->?`` / ``->{type}``).
- BM25 full-text search (``@@`` + ``search::score(0)``).
- Paginated listing (offset-based with overflow detection).
- ``retrieve_graph`` orchestration (search → BFS → dedup → sort).
- Health check (connected, disconnected, unreachable).

Every test uses a mocked ``AsyncSurreal`` so no SurrealDB instance is needed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from surrealdb import RecordID

from core.exceptions import ExternalServiceError
from packages.graph_backend.surrealdb import (
    SurrealGraphBackend,
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


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_surreal() -> AsyncMock:
    """A mocked ``AsyncSurreal`` that returns empty results by default."""
    surreal = AsyncMock()
    surreal.query.return_value = [[]]
    return surreal


@pytest.fixture
def backend(mock_surreal: AsyncMock) -> SurrealGraphBackend:
    """A ``SurrealGraphBackend`` with schema bootstrap pre-completed."""
    bk = SurrealGraphBackend(surreal=mock_surreal)
    bk._schema_ensured = True
    return bk


@pytest.fixture
def setup_traverse(mock_surreal: AsyncMock) -> Callable[..., None]:
    """Configure ``mock_surreal.query`` for BFS-traversal tests.

    Usage in a traverse test::

        setup_traverse(
            entities={
                str(uuid): {"id": RecordID("entity", str(uuid)), …},
            },
            neighbors={
                str(uuid): ["neighbor-uuid", …],
            },
        )

    The ``entities`` dict maps entity-id strings to the full dict that
    ``get_entity`` should return.  The ``neighbors`` dict maps entity-id
    strings to lists of neighbor-id strings that ``->?->entity.id`` should
    return.  The side-effect function inspects each ``_surreal.query`` call
    to distinguish entity-fetch (``SELECT * FROM entity``) from neighbour
    discovery (``SELECT VALUE``) and dispatches accordingly.
    """
    _entities: dict[str, dict] = {}
    _neighbors: dict[str, list[str]] = {}

    def configure(*, entities: dict[str, dict], neighbors: dict[str, list[str]]) -> None:
        _entities.clear()
        _entities.update(entities)
        _neighbors.clear()
        _neighbors.update(neighbors)

        async def side_effect(query: str, params: dict[str, Any] | None = None) -> list[list]:
            if not params:
                return [[]]

            # Neighbour discovery  (SELECT VALUE ->?->entity.id FROM $current_id)
            if "SELECT VALUE" in query:
                cid = params.get("current_id")
                eid = str(cid.id) if hasattr(cid, "id") else ""
                nids = _neighbors.get(eid, [])
                return [[RecordID("entity", n) for n in nids]]

            # Entity fetch  (SELECT * FROM entity WHERE id = $id …)
            if "SELECT" in query and "FROM entity" in query:
                eid_param = params.get("id")
                eid = str(eid_param.id) if hasattr(eid_param, "id") else ""
                rec = _entities.get(eid)
                return [[rec]] if rec else [[]]

            return [[]]

        mock_surreal.query.side_effect = side_effect

    return configure


# ── Entity record factory ────────────────────────────────────────────────────


def make_entity_record(
    entity_id: UUID | None = None,
    name: str = "test-entity",
    entity_type: str = "Person",
    summary: str = "A test entity",
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a dict that mimics a SurrealDB entity row."""
    eid = entity_id or ENTITY_ID
    return {
        "id": RecordID("entity", str(eid)),
        "organization_id": str(ORG_ID),
        "project_id": str(PROJ_ID),
        "name": name,
        "entity_type": entity_type,
        "summary": summary,
        "attributes": attributes or {},
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
    }


# ── Test Cases ───────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestSurrealGraphBackendSanitize:
    """``_sanitize_edge_type`` validation."""

    @staticmethod
    def test_sanitize_edge_type_valid() -> None:
        """Valid names: alphanumeric + underscores only."""
        assert SurrealGraphBackend._sanitize_edge_type("likes") == "likes"
        assert SurrealGraphBackend._sanitize_edge_type("has_role_123") == "has_role_123"

    @staticmethod
    def test_sanitize_edge_type_invalid() -> None:
        """Invalid names: spaces, dashes, leading digits, empty."""
        for bad in ("two words", "dash-ed", "123abc", ""):
            with pytest.raises(ValueError, match="Unsafe edge type"):
                SurrealGraphBackend._sanitize_edge_type(bad)

    @staticmethod
    def test_sanitize_edge_type_sql_injection() -> None:
        """Known SQL-like patterns are rejected."""
        for bad in ("DROP TABLE", "1; SELECT *", "'; DELETE"):
            with pytest.raises(ValueError, match="Unsafe edge type"):
                SurrealGraphBackend._sanitize_edge_type(bad)


@pytest.mark.unit
class TestSurrealGraphBackendHelpers:
    """Static / class helper methods and cursor utilities."""

    @staticmethod
    def test_record_id_to_str() -> None:
        """RecordID → string id, plain string → itself, None → ''."""
        rid = RecordID("entity", "abc-123")
        assert SurrealGraphBackend._record_id_to_str(rid) == "abc-123"
        assert SurrealGraphBackend._record_id_to_str("plain-string") == "plain-string"
        assert SurrealGraphBackend._record_id_to_str(None) == ""

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
    def test_require_connection_raises(backend: SurrealGraphBackend) -> None:
        """``_require_connection`` raises ``ExternalServiceError`` when
        ``_surreal`` is ``None``.
        """
        bk = SurrealGraphBackend(surreal=None)
        with pytest.raises(ExternalServiceError, match="SurrealDB not connected"):
            bk._require_connection()

        # Connected backend should not raise
        backend._require_connection()  # no error


@pytest.mark.unit
class TestSurrealGraphBackendEntityCrud:
    """Entity create, get, delete, update — SurrealQL structure + return shape."""

    # ── create_entity ─────────────────────────────────────────────────────

    @staticmethod
    async def test_create_entity(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """New entity: verifies CREATE pattern in SurrealQL and return shape."""
        record = make_entity_record(name="test", entity_type="Person")
        mock_surreal.query.return_value = [[], [record]]

        result = await backend.create_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            name="Test",
            entity_type="Person",
        )

        # SurrealQL contains the upsert pattern
        call = mock_surreal.query.call_args
        query = call[0][0]
        assert "CREATE entity SET" in query
        assert "IF array::len($existing) > 0 THEN" in query
        assert "UPDATE entity SET" in query

        # Params: name is lowercased
        params = call[0][1]
        assert params["name"] == "test"
        assert params["type"] == "Person"

        # Return shape
        assert result["id"] == str(ENTITY_ID)
        assert result["name"] == "test"
        assert result["type"] == "Person"

    @staticmethod
    async def test_create_entity_name_normalized(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """Entity name is lowered and stripped before query."""
        record = make_entity_record(name="  MixedCase  ", entity_type="Custom")
        mock_surreal.query.return_value = [[], [record]]

        await backend.create_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            name="  MixedCase  ",
            entity_type="Custom",
        )

        params = mock_surreal.query.call_args[0][1]
        assert params["name"] == "mixedcase"

    @staticmethod
    async def test_create_entity_db_error(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """SurrealDB error is wrapped in ``ExternalServiceError``."""
        mock_surreal.query.side_effect = RuntimeError("DB connection lost")

        with pytest.raises(ExternalServiceError, match="DB connection lost"):
            await backend.create_entity(
                org_id=ORG_ID,
                project_id=PROJ_ID,
                name="Test",
                entity_type="Person",
            )

    # ── get_entity ────────────────────────────────────────────────────────

    @staticmethod
    @pytest.mark.parametrize(
        ("scenario", "query_return", "expected"),
        [
            ("found", [[make_entity_record()]], "test-entity"),
            ("not_found", [[]], None),
        ],
    )
    async def test_get_entity(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
        scenario: str,
        query_return: list[list],
        expected: Any,
    ) -> None:
        """Entity retrieval returns dict or None."""
        mock_surreal.query.return_value = query_return

        result = await backend.get_entity(ORG_ID, PROJ_ID, ENTITY_ID)

        if expected is None:
            assert result is None
        else:
            assert result is not None
            assert result["name"] == expected
            assert result["id"] == str(ENTITY_ID)

    # ── delete_entity ─────────────────────────────────────────────────────

    @staticmethod
    @pytest.mark.parametrize(
        ("scenario", "query_return", "expected"),
        [
            ("exists", [[{"id": RecordID("entity", str(ENTITY_ID))}]], True),
            ("not_exists", [[]], False),
        ],
    )
    async def test_delete_entity(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
        scenario: str,
        query_return: list[list],
        expected: bool,
    ) -> None:
        """Delete returns True or False based on SurrealQL result."""
        mock_surreal.query.return_value = query_return

        result = await backend.delete_entity(ORG_ID, PROJ_ID, ENTITY_ID)
        assert result is expected

    # ── update_entity ─────────────────────────────────────────────────────

    @staticmethod
    async def test_update_entity_partial(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """Only provided fields are included in the SET clause."""
        record = make_entity_record(name="updated-name")
        mock_surreal.query.return_value = [[record]]

        result = await backend.update_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
            name="Updated-Name",
        )

        query = mock_surreal.query.call_args[0][0]
        assert "name = $name" in query
        assert "summary = $summary" not in query  # not provided
        assert "entity_type = $entity_type" not in query
        assert "attributes = $attributes" not in query

        assert result is not None
        assert result["name"] == "updated-name"

    @staticmethod
    async def test_update_entity_not_found(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """Returns None when entity does not exist."""
        mock_surreal.query.return_value = [[]]

        result = await backend.update_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
            name="Anything",
        )
        assert result is None

    @staticmethod
    async def test_update_entity_no_fields(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """No fields to update → falls through to ``get_entity``."""
        # Normal get_entity would call _surreal.query; verify it was called
        mock_surreal.query.return_value = [[make_entity_record()]]

        result = await backend.update_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
        )

        # Should have called get_entity which calls _surreal.query
        assert mock_surreal.query.called
        # But should NOT have called _surreal.query with "UPDATE"
        query = mock_surreal.query.call_args[0][0]
        assert "UPDATE" not in query

        assert result is not None
        assert result["id"] == str(ENTITY_ID)


@pytest.mark.unit
class TestSurrealGraphBackendRelationships:
    """Relationship create with RELATE/UPDATE branching."""

    @staticmethod
    async def test_create_relationship_new(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """New relationship issues RELATE."""
        src_rid = RecordID("entity", str(ENTITY_ID))
        tgt_rid = RecordID("entity", str(TARGET_ID))
        edge_id = RecordID("likes", str(uuid4()))

        edge_record = {
            "id": edge_id,
            "in": src_rid,
            "out": tgt_rid,
            "properties": {},
            "fact": "",
            "confidence": 1.0,
            "valid_from": None,
            "valid_to": None,
            "created_at": "2024-01-01T00:00:00",
        }
        mock_surreal.query.return_value = [[], [edge_record]]

        result = await backend.create_relationship(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            source_id=ENTITY_ID,
            target_id=TARGET_ID,
            relationship_type="likes",
        )

        query = mock_surreal.query.call_args[0][0]
        assert "RELATE $source_id -> likes -> $target_id" in query
        assert result["source_id"] == str(ENTITY_ID)
        assert result["target_id"] == str(TARGET_ID)
        # The type is extracted from the edge RecordID's table_name
        assert result["type"] == "likes"

    @staticmethod
    async def test_create_relationship_existing(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """Existing relationship issues UPDATE on the edge."""
        src_rid = RecordID("entity", str(ENTITY_ID))
        tgt_rid = RecordID("entity", str(TARGET_ID))
        edge_id = RecordID("likes", str(uuid4()))

        edge_record = {
            "id": edge_id,
            "in": src_rid,
            "out": tgt_rid,
            "properties": {"strength": 5},
            "fact": "",
            "confidence": 1.0,
            "valid_from": None,
            "valid_to": None,
            "created_at": "2024-01-01T00:00:00",
        }
        mock_surreal.query.return_value = [[], [edge_record]]

        result = await backend.create_relationship(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            source_id=ENTITY_ID,
            target_id=TARGET_ID,
            relationship_type="likes",
        )

        query = mock_surreal.query.call_args[0][0]
        # Even for "existing", the SurrealQL always contains both branches
        assert "UPDATE $existing[0].id SET" in query
        assert "RELATE $source_id -> likes -> $target_id" in query
        assert result is not None

    @staticmethod
    async def test_create_relationship_invalid_type(
        backend: SurrealGraphBackend,
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
    async def test_create_relationship_error(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """SurrealDB error is wrapped in ``ExternalServiceError``."""
        mock_surreal.query.side_effect = RuntimeError("surreal timeout")

        with pytest.raises(ExternalServiceError, match="surreal timeout"):
            await backend.create_relationship(
                org_id=ORG_ID,
                project_id=PROJ_ID,
                source_id=ENTITY_ID,
                target_id=TARGET_ID,
                relationship_type="likes",
            )


@pytest.mark.unit
class TestSurrealGraphBackendTraversal:
    """BFS traversal with native SurrealQL arrow syntax.

    Uses the ``setup_traverse`` fixture (Option B) — a side-effect function
    on ``mock_surreal.query`` that dispatches to entity records or neighbour
    ID lists based on the query text.
    """

    @staticmethod
    async def test_traverse_single_hop(
        backend: SurrealGraphBackend,
        setup_traverse: Callable[..., None],
    ) -> None:
        """Start node + one neighbour are returned at depths 0 and 1."""
        setup_traverse(
            entities={
                str(ENTITY_ID): make_entity_record(entity_id=ENTITY_ID, name="Start"),
                str(NEIGHBOR_ID): make_entity_record(
                    entity_id=NEIGHBOR_ID, name="Neighbor"
                ),
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
    async def test_traverse_with_edge_types(
        backend: SurrealGraphBackend,
        setup_traverse: Callable[..., None],
    ) -> None:
        """Specific edge types: queries each edge table individually."""
        setup_traverse(
            entities={
                str(ENTITY_ID): make_entity_record(entity_id=ENTITY_ID, name="Start"),
                str(NEIGHBOR_ID): make_entity_record(
                    entity_id=NEIGHBOR_ID, name="Neighbor"
                ),
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

        # Should have queried the "likes" edge table via ->likes->entity.id
        query = backend._surreal.query.call_args[0][0]
        assert "->likes->entity.id" in query

    @staticmethod
    async def test_traverse_empty_edge_types(
        backend: SurrealGraphBackend,
    ) -> None:
        """Empty edge_types list returns just the start node (no BFS)."""
        # We need to mock get_entity — it is called inside the empty edge_types branch
        bk = backend
        start_record = make_entity_record(entity_id=ENTITY_ID, name="Start")
        bk.get_entity = AsyncMock(return_value={
            "id": str(ENTITY_ID),
            "name": "Start",
            "type": "Person",
            "summary": "",
            "attributes": {},
            "created_at": "2024-01-01T00:00:00",
        })

        result = await bk.traverse(
            ORG_ID,
            PROJ_ID,
            ENTITY_ID,
            max_depth=2,
            edge_types=[],
        )

        assert len(result) == 1
        assert result[0]["id"] == str(ENTITY_ID)
        assert result[0]["depth"] == 0
        # _surreal.query should NOT have been called (no neighbor discovery)
        bk._surreal.query.assert_not_called()

    @staticmethod
    async def test_traverse_respects_max_depth(
        backend: SurrealGraphBackend,
        setup_traverse: Callable[..., None],
    ) -> None:
        """BFS does not go beyond ``max_depth``."""
        setup_traverse(
            entities={
                str(ENTITY_ID): make_entity_record(entity_id=ENTITY_ID, name="Start"),
                str(NEIGHBOR_ID): make_entity_record(
                    entity_id=NEIGHBOR_ID, name="Neighbor"
                ),
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
        backend: SurrealGraphBackend,
        setup_traverse: Callable[..., None],
    ) -> None:
        """Start node not found → empty list."""
        setup_traverse(
            entities={},  # no entities exist
            neighbors={},
        )

        result = await backend.traverse(ORG_ID, PROJ_ID, ENTITY_ID, max_depth=2)
        assert result == []


@pytest.mark.unit
class TestSurrealGraphBackendSearchAndListing:
    """BM25 full-text search and paginated entity/edge listing."""

    # ── search_entities ─────────────────────────────────────────────────────

    @staticmethod
    async def test_search_entities(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """Search uses ``@@`` operator and ``search::score(0)``."""
        record = make_entity_record(name="findable", summary="something")
        record["score"] = 0.85
        mock_surreal.query.return_value = [[record]]

        result = await backend.search_entities(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            query="findable",
        )

        query = mock_surreal.query.call_args[0][0]
        assert "@@" in query
        assert "search::score(0)" in query
        assert len(result) == 1
        assert result[0]["score"] == 0.85

    @staticmethod
    async def test_search_entities_no_results(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """No matches → empty list."""
        mock_surreal.query.return_value = [[]]

        result = await backend.search_entities(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            query="nothing",
        )
        assert result == []

    # ── list_entities ──────────────────────────────────────────────────────

    @staticmethod
    async def test_list_entities_no_pagination(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """Fewer items than limit → no overflow, no cursor."""
        records = [
            make_entity_record(entity_id=ENTITY_ID, name="A"),
            make_entity_record(entity_id=NEIGHBOR_ID, name="B"),
        ]
        mock_surreal.query.return_value = [records]  # 2 items, limit=3 → no overflow

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
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """More items than limit → overflow detected, cursor returned."""
        records = [
            make_entity_record(entity_id=UUID(f"00000000-0000-0000-0000-{i:012d}"), name=f"E{i}")
            for i in range(6)
        ]
        mock_surreal.query.return_value = [records]  # 6 items, limit=5 → overflow

        result = await backend.list_entities(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            limit=5,
        )

        assert len(result["items"]) == 5
        assert result["has_more"] is True
        assert result["next_cursor"] is not None

    # ── list_entity_edges ──────────────────────────────────────────────────

    @staticmethod
    async def test_list_entity_edges_with_predicate(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """With predicate: queries specific edge table."""
        src_rid = RecordID("entity", str(ENTITY_ID))
        tgt_rid = RecordID("entity", str(TARGET_ID))
        edge_id = RecordID("likes", str(uuid4()))

        records = [
            {
                "id": edge_id,
                "in": src_rid,
                "out": tgt_rid,
                "properties": {},
                "fact": "",
                "confidence": 1.0,
                "valid_from": None,
                "valid_to": None,
                "created_at": "2024-01-01T00:00:00",
                "edge_table_name": "likes",
            }
        ]
        mock_surreal.query.return_value = [records]

        result = await backend.list_entity_edges(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=ENTITY_ID,
            predicate="likes",
        )

        query = mock_surreal.query.call_args[0][0]
        assert "->likes" in query
        assert len(result["items"]) == 1
        assert result["items"][0]["source_id"] == str(ENTITY_ID)
        assert result["items"][0]["target_id"] == str(TARGET_ID)


@pytest.mark.unit
class TestSurrealGraphBackendRetrieveGraph:
    """``retrieve_graph`` — search → BFS → dedup → sort by distance."""

    @staticmethod
    async def test_retrieve_graph(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """Matches search, then BFS-traverses outward, deduplicates, sorts."""
        # Mock search_entities to return 1 match
        match_entity = {
            "id": str(ENTITY_ID),
            "name": "Match",
            "type": "Person",
            "summary": "found entity",
        }
        backend.search_entities = AsyncMock(return_value=[match_entity])

        # Mock traverse to return the match + 1 neighbor
        neighbor = {
            "id": str(NEIGHBOR_ID),
            "name": "Neighbor",
            "type": "Person",
            "summary": "",
            "depth": 1,
        }
        backend.traverse = AsyncMock(return_value=[neighbor])

        result = await backend.retrieve_graph(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            query="find",
            match_limit=5,
            max_depth=2,
            max_results=50,
        )

        # Search called with correct params
        backend.search_entities.assert_called_once_with(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            query="find",
            limit=5,
        )

        # Traverse called from the matched entity
        backend.traverse.assert_called_once_with(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            start_node_id=ENTITY_ID,
            max_depth=2,
        )

        # Results deduped, sorted by distance
        assert len(result) == 2
        assert result[0]["id"] == str(ENTITY_ID)
        assert result[0]["distance"] == 0
        assert result[1]["id"] == str(NEIGHBOR_ID)
        assert result[1]["distance"] == 1

    @staticmethod
    async def test_retrieve_graph_no_matches(
        backend: SurrealGraphBackend,
    ) -> None:
        """No search matches → empty list."""
        backend.search_entities = AsyncMock(return_value=[])

        result = await backend.retrieve_graph(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            query="nothing",
        )

        assert result == []

    @staticmethod
    async def test_retrieve_graph_traverse_failure_graceful(
        backend: SurrealGraphBackend,
    ) -> None:
        """Traverse failure is caught and logged; matched entity still returned."""
        match_entity = {
            "id": str(ENTITY_ID),
            "name": "Match",
            "type": "Person",
            "summary": "",
        }
        backend.search_entities = AsyncMock(return_value=[match_entity])
        backend.traverse = AsyncMock(side_effect=ValueError("traverse failed"))

        result = await backend.retrieve_graph(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            query="find",
        )

        # Only the matched entity should be returned (traverse failed gracefully)
        assert len(result) == 1
        assert result[0]["id"] == str(ENTITY_ID)
        assert result[0]["distance"] == 0


@pytest.mark.unit
class TestSurrealGraphBackendHealthCheck:
    """Health-check scenarios."""

    @staticmethod
    async def test_health_check_ok(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """SurrealDB reachable → ``True``."""
        mock_surreal.query.return_value = [[{"1": 1}]]
        assert await backend.health_check() is True
        mock_surreal.query.assert_called_once_with("SELECT 1;")

    @staticmethod
    async def test_health_check_fail(
        backend: SurrealGraphBackend,
        mock_surreal: AsyncMock,
    ) -> None:
        """SurrealDB unreachable → ``False``."""
        mock_surreal.query.side_effect = RuntimeError("connection refused")
        assert await backend.health_check() is False

    @staticmethod
    async def test_health_check_not_connected() -> None:
        """No SurrealDB connection → ``False``."""
        bk = SurrealGraphBackend(surreal=None)
        assert await bk.health_check() is False
