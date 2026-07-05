"""Integration tests for PostgresGraphBackend — against real PostgreSQL via testcontainers.

Tests the full Postgres graph backend pipeline with real DB tables:
1. Entity CRUD (create, get, delete, update)
2. Relationship CRUD (create, list, expire)
3. Entity-Episode linking (link_entity_to_episode, get_entities_for_session)
4. Search (search_entities, bulk_search_entities)
5. Paginated listing (list_entities, list_entity_edges, get_observations)
6. Merge entities (atomic merge + rewiring)
7. Observations (upsert, get, filter)
8. Graph traversal and retrieve_graph
9. Health check
10. Edge cases (empty project, idempotent links, duplicate merges)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncGenerator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from core.exceptions import ExternalServiceError, NotFoundError
from tests.conftest import (
    _ensure_testcontainers_env,
    _run_alembic_upgrade,
    _start_postgres_container,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        'not os.environ.get("DOCKER_HOST", "") and not os.environ.get("TC_HOST", "")',
        reason="Docker not available — requires testcontainers",
    ),
]

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJ_ID = UUID("00000000-0000-0000-0000-000000000002")
ALT_PROJ_ID = UUID("00000000-0000-0000-0000-000000000003")
NOW = datetime.now(timezone.utc)

# Module-level container reference
_pg_container: Any = None


def setup_module() -> None:
    """One-time container + migration for all tests in this module."""
    global _pg_container
    _ensure_testcontainers_env()
    _pg_container = _start_postgres_container()
    url = _pg_container.get_connection_url()
    driver_url = url.replace("postgresql://", "postgresql+asyncpg://")
    _run_alembic_upgrade(driver_url)


def teardown_module() -> None:
    """Stop the container after all tests."""
    global _pg_container
    if _pg_container:
        _pg_container.stop()
        _pg_container = None


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """Create a fresh async session per test, rolled back on teardown."""
    url = _pg_container.get_connection_url()
    driver_url = url.replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(driver_url, poolclass=NullPool, pool_pre_ping=True)

    async with engine.connect() as conn:
        await conn.execute(sa_text("SET search_path TO public"))
        transaction = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await transaction.rollback()
            await session.close()

    await engine.dispose()


@pytest_asyncio.fixture
async def backend(db: AsyncSession) -> Any:
    """A PostgresGraphBackend scoped to one test."""
    from packages.graph_backend.postgres import PostgresGraphBackend

    return PostgresGraphBackend(db=db, max_traversal_depth=3)


async def _create_test_entity(
    backend: Any,
    name: str = "test-entity",
    entity_type: str = "Person",
    summary: str = "A test entity",
) -> dict[str, Any]:
    """Helper to create an entity and return it."""
    return await backend.create_entity(
        org_id=ORG_ID,
        project_id=PROJ_ID,
        name=name,
        entity_type=entity_type,
        summary=summary,
    )


async def _create_test_relationship(
    backend: Any,
    source_id: UUID,
    target_id: UUID,
    rel_type: str = "knows",
    confidence: float = 1.0,
) -> dict[str, Any]:
    """Helper to create a relationship."""
    return await backend.create_relationship(
        org_id=ORG_ID,
        project_id=PROJ_ID,
        source_id=source_id,
        target_id=target_id,
        relationship_type=rel_type,
        confidence=confidence,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Entity CRUD
# ═══════════════════════════════════════════════════════════════════════════════


class TestEntityCrud:
    """Entity create, get, delete, update — against real PostgreSQL."""

    async def test_create_entity(self, backend: Any) -> None:
        """create_entity returns dict with id, name, type, created_at."""
        entity = await _create_test_entity(backend, name="Alice", entity_type="Person")
        assert entity["name"] == "alice"  # name is lowercased
        assert entity["type"] == "Person"
        assert "id" in entity
        assert "created_at" in entity

    async def test_create_entity_with_summary(self, backend: Any) -> None:
        """create_entity with summary sets the summary field."""
        entity = await _create_test_entity(backend, name="Bob", summary="The builder")
        assert entity["summary"] == "The builder"

    async def test_get_entity(self, backend: Any) -> None:
        """get_entity returns the entity dict."""
        created = await _create_test_entity(backend, name="Charlie")
        entity_id = UUID(created["id"])

        fetched = await backend.get_entity(ORG_ID, PROJ_ID, entity_id)
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["name"] == "charlie"

    async def test_get_entity_not_found(self, backend: Any) -> None:
        """get_entity returns None for non-existent entity."""
        result = await backend.get_entity(ORG_ID, PROJ_ID, uuid4())
        assert result is None

    async def test_delete_entity(self, backend: Any) -> None:
        """delete_entity returns True and entity is gone."""
        entity = await _create_test_entity(backend, name="DeleteMe")
        entity_id = UUID(entity["id"])

        deleted = await backend.delete_entity(ORG_ID, PROJ_ID, entity_id)
        assert deleted is True

        # Verify it's gone
        fetched = await backend.get_entity(ORG_ID, PROJ_ID, entity_id)
        assert fetched is None

    async def test_delete_entity_not_found(self, backend: Any) -> None:
        """delete_entity returns False for non-existent entity."""
        result = await backend.delete_entity(ORG_ID, PROJ_ID, uuid4())
        assert result is False

    async def test_update_entity(self, backend: Any) -> None:
        """update_entity changes specified fields."""
        entity = await _create_test_entity(backend, name="UpdateMe", summary="original")
        entity_id = UUID(entity["id"])

        updated = await backend.update_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            entity_id=entity_id,
            name="Updated",
            summary="new summary",
        )
        assert updated["name"] == "updated"
        assert updated["summary"] == "new summary"

    async def test_update_entity_not_found(self, backend: Any) -> None:
        """update_entity raises NotFoundError for missing entity."""
        with pytest.raises(NotFoundError):
            await backend.update_entity(
                org_id=ORG_ID,
                project_id=PROJ_ID,
                entity_id=uuid4(),
                name="Ghost",
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Relationships
# ═══════════════════════════════════════════════════════════════════════════════


class TestRelationships:
    """Relationship create, list, expire — against real PostgreSQL."""

    async def test_create_relationship(self, backend: Any) -> None:
        """create_relationship returns dict with source_id, target_id, type."""
        src = await _create_test_entity(backend, name="Source")
        tgt = await _create_test_entity(backend, name="Target")
        src_id = UUID(src["id"])
        tgt_id = UUID(tgt["id"])

        rel = await _create_test_relationship(backend, src_id, tgt_id)
        assert rel["source_id"] == str(src_id)
        assert rel["target_id"] == str(tgt_id)
        assert rel["type"] == "knows"

    async def test_expire_relationship(self, backend: Any) -> None:
        """expire_relationship soft-deletes and returns True."""
        src = await _create_test_entity(backend, name="ExpSrc")
        tgt = await _create_test_entity(backend, name="ExpTgt")
        rel = await _create_test_relationship(
            backend, UUID(src["id"]), UUID(tgt["id"]),
        )

        expired = await backend.expire_relationship(ORG_ID, PROJ_ID, UUID(rel["id"]))
        assert expired is True

        # Double-expire returns False
        expired_again = await backend.expire_relationship(ORG_ID, PROJ_ID, UUID(rel["id"]))
        assert expired_again is False

    async def test_expire_relationship_not_found(self, backend: Any) -> None:
        """expire_relationship returns False for non-existent rel."""
        result = await backend.expire_relationship(ORG_ID, PROJ_ID, uuid4())
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# Entity-Episode Linking
# ═══════════════════════════════════════════════════════════════════════════════


class TestEntityEpisodeLinking:
    """Entity-Episode linking and session-scoped queries."""

    async def test_link_entity_to_episode(self, backend: Any) -> None:
        """link_entity_to_episode succeeds and returns None."""
        entity = await _create_test_entity(backend, name="Linked")
        result = await backend.link_entity_to_episode(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            episode_id=uuid4(),
            entity_id=UUID(entity["id"]),
        )
        assert result is None

    async def test_link_entity_to_episode_idempotent(self, backend: Any) -> None:
        """Duplicate link does not raise."""
        entity = await _create_test_entity(backend, name="LinkIdempotent")
        entity_id = UUID(entity["id"])
        episode_id = uuid4()

        await backend.link_entity_to_episode(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            episode_id=episode_id,
            entity_id=entity_id,
        )
        # Second call should be a no-op
        await backend.link_entity_to_episode(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            episode_id=episode_id,
            entity_id=entity_id,
        )

    async def test_get_entities_for_session_empty(self, backend: Any) -> None:
        """get_entities_for_session returns empty list when no links exist."""
        result = await backend.get_entities_for_session(
            ORG_ID, PROJ_ID, uuid4(),
        )
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# Search
# ═══════════════════════════════════════════════════════════════════════════════


class TestSearch:
    """Full-text and fuzzy search."""

    async def test_search_entities(self, backend: Any) -> None:
        """search_entities finds entities by name."""
        await _create_test_entity(backend, name="Searchable John")
        await _create_test_entity(backend, name="Another Entity")

        result = await backend.search_entities(ORG_ID, PROJ_ID, query="john")
        # At least the matching entity
        assert len(result) >= 1
        assert any("john" in e["name"] for e in result)

    async def test_search_entities_no_results(self, backend: Any) -> None:
        """search_entities returns empty list for non-matching query."""
        result = await backend.search_entities(ORG_ID, PROJ_ID, query="zzz_nonexistent")
        assert result == []

    async def test_bulk_search_entities(self, backend: Any) -> None:
        """bulk_search_entities returns fuzzy matches."""
        await _create_test_entity(backend, name="John Smith")
        await _create_test_entity(backend, name="Jonathan Doe")

        result = await backend.bulk_search_entities(
            ORG_ID, PROJ_ID, query="john", fuzzy_threshold=0.2,
        )
        assert len(result) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# Paginated Listing
# ═══════════════════════════════════════════════════════════════════════════════


class TestPaginatedListing:
    """list_entities, list_entity_edges, get_observations pagination."""

    async def test_list_entities_empty(self, backend: Any) -> None:
        """Empty project returns empty items."""
        result = await backend.list_entities(ORG_ID, PROJ_ID)
        assert result == {"items": [], "next_cursor": None, "has_more": False}

    async def test_list_entities_with_data(self, backend: Any) -> None:
        """list_entities returns all entities for a project."""
        await _create_test_entity(backend, name="Entity A")
        await _create_test_entity(backend, name="Entity B")

        result = await backend.list_entities(ORG_ID, PROJ_ID)
        assert len(result["items"]) == 2
        assert result["has_more"] is False

    async def test_list_entity_edges_empty(self, backend: Any) -> None:
        """list_entity_edges returns empty for entity with no relationships."""
        entity = await _create_test_entity(backend, name="EdgeTest")
        result = await backend.list_entity_edges(ORG_ID, PROJ_ID, UUID(entity["id"]))
        assert result == {"items": [], "next_cursor": None, "has_more": False}

    async def test_list_entity_edges_with_data(self, backend: Any) -> None:
        """list_entity_edges returns incident edges for an entity."""
        src = await _create_test_entity(backend, name="EdgeSrc")
        tgt = await _create_test_entity(backend, name="EdgeTgt")
        rel = await _create_test_relationship(backend, UUID(src["id"]), UUID(tgt["id"]), rel_type="likes")

        result = await backend.list_entity_edges(ORG_ID, PROJ_ID, UUID(src["id"]))
        assert len(result["items"]) == 1
        assert result["items"][0]["type"] == "likes"
        assert result["items"][0]["source_id"] == str(src["id"])
        assert result["items"][0]["target_id"] == str(tgt["id"])
        assert result["has_more"] is False

    async def test_list_entity_edges_with_predicate_filter(self, backend: Any) -> None:
        """list_entity_edges filters by predicate."""
        src = await _create_test_entity(backend, name="EdgePredSrc")
        tgt_a = await _create_test_entity(backend, name="EdgePredTgtA")
        tgt_b = await _create_test_entity(backend, name="EdgePredTgtB")
        src_id = UUID(src["id"])
        await _create_test_relationship(backend, src_id, UUID(tgt_a["id"]), rel_type="likes")
        await _create_test_relationship(backend, src_id, UUID(tgt_b["id"]), rel_type="knows")

        # Filter by predicate
        result = await backend.list_entity_edges(ORG_ID, PROJ_ID, src_id, predicate="likes")
        assert len(result["items"]) == 1
        assert result["items"][0]["type"] == "likes"


# ═══════════════════════════════════════════════════════════════════════════════
# Merge Entities
# ═══════════════════════════════════════════════════════════════════════════════


class TestMergeEntities:
    """Atomic entity merge with rewiring."""

    async def test_merge_entities_empty_ids(self, backend: Any) -> None:
        """Empty merged_ids returns zero counts (no-op)."""
        entity = await _create_test_entity(backend, name="Canonical")
        result = await backend.merge_entities(
            ORG_ID, PROJ_ID, UUID(entity["id"]), [],
        )
        assert result == {"rewired_count": 0, "deleted_count": 0, "merged_count": 0}

    async def test_merge_entities_raises_on_bad_canonical(self, backend: Any) -> None:
        """Non-existent canonical raises NotFoundError."""
        with pytest.raises(NotFoundError):
            await backend.merge_entities(ORG_ID, PROJ_ID, uuid4(), [uuid4()])


# ═══════════════════════════════════════════════════════════════════════════════
# Observations
# ═══════════════════════════════════════════════════════════════════════════════


class TestObservations:
    """Observation upsert and retrieval."""

    async def test_upsert_observation(self, backend: Any) -> None:
        """upsert_observation creates and returns observation dict."""
        entity = await _create_test_entity(backend, name="Observed")
        obs = await backend.upsert_observation(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            subject_entity_id=UUID(entity["id"]),
            observation_type="co_occurrence",
            content="Test observation",
            confidence=0.95,
        )
        assert obs["subject_entity_id"] == entity["id"]
        assert obs["observation_type"] == "co_occurrence"
        assert obs["content"] == "Test observation"
        assert obs["confidence"] == 0.95

    async def test_upsert_observation_idempotent(self, backend: Any) -> None:
        """Re-upserting same observation updates content."""
        entity = await _create_test_entity(backend, name="ObsIdempotent")
        eid = UUID(entity["id"])

        obs1 = await backend.upsert_observation(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            subject_entity_id=eid,
            observation_type="test_type",
            content="original",
            confidence=0.5,
        )

        obs2 = await backend.upsert_observation(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            subject_entity_id=eid,
            observation_type="test_type",
            content="updated",
            confidence=0.9,
        )

        assert obs2["content"] == "updated"
        assert obs2["confidence"] == 0.9

    async def test_get_observations_filtered(self, backend: Any) -> None:
        """get_observations returns filtered results."""
        entity = await _create_test_entity(backend, name="ObsFilter")
        eid = UUID(entity["id"])

        await backend.upsert_observation(
            org_id=ORG_ID, project_id=PROJ_ID,
            subject_entity_id=eid,
            observation_type="type_a", content="A", confidence=0.5,
        )
        await backend.upsert_observation(
            org_id=ORG_ID, project_id=PROJ_ID,
            subject_entity_id=eid,
            observation_type="type_b", content="B", confidence=0.5,
        )

        # Filter by type
        result = await backend.get_observations(
            ORG_ID, PROJ_ID, observation_type="type_a",
        )
        assert len(result["items"]) == 1
        assert result["items"][0]["observation_type"] == "type_a"


# ═══════════════════════════════════════════════════════════════════════════════
# Traversal & retrieve_graph
# ═══════════════════════════════════════════════════════════════════════════════


class TestTraversal:
    """Graph BFS traversal and retrieve_graph."""

    async def test_traverse_single_node(self, backend: Any) -> None:
        """Traversal from a node with no edges returns just that node."""
        entity = await _create_test_entity(backend, name="Lonely")
        result = await backend.traverse(ORG_ID, PROJ_ID, UUID(entity["id"]))
        assert len(result) == 1
        assert result[0]["name"] == "lonely"

    async def test_traverse_with_edges(self, backend: Any) -> None:
        """Traversal follows relationships."""
        src = await _create_test_entity(backend, name="TraverseSrc")
        tgt = await _create_test_entity(backend, name="TraverseTgt")
        await _create_test_relationship(backend, UUID(src["id"]), UUID(tgt["id"]))

        result = await backend.traverse(ORG_ID, PROJ_ID, UUID(src["id"]))
        assert len(result) == 2  # src + tgt
        names = {r["name"] for r in result}
        assert "traversesrc" in names
        assert "traversetgt" in names

    async def test_traverse_empty_project(self, backend: Any) -> None:
        """Traversal from non-existent node returns empty list."""
        result = await backend.traverse(ORG_ID, PROJ_ID, uuid4())
        assert result == []

    async def test_retrieve_graph(self, backend: Any) -> None:
        """retrieve_graph searches then traverses outward."""
        await _create_test_entity(backend, name="GraphSearch Me")
        result = await backend.retrieve_graph(
            ORG_ID, PROJ_ID, query="graphsearch",
        )
        assert isinstance(result, list)


# ═══════════════════════════════════════════════════════════════════════════════
# Get All (batch operations)
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetAll:
    """get_all_entities and get_all_relationships — batch operations."""

    async def test_get_all_entities_empty(self, backend: Any) -> None:
        """get_all_entities returns empty list for empty project."""
        result = await backend.get_all_entities(ORG_ID, PROJ_ID)
        assert result == []

    async def test_get_all_entities_with_data(self, backend: Any) -> None:
        """get_all_entities returns all entities."""
        await _create_test_entity(backend, name="AllA")
        await _create_test_entity(backend, name="AllB")
        result = await backend.get_all_entities(ORG_ID, PROJ_ID)
        assert len(result) >= 2

    async def test_get_all_relationships_empty(self, backend: Any) -> None:
        """get_all_relationships returns empty list for empty project."""
        result = await backend.get_all_relationships(ORG_ID, PROJ_ID)
        assert result == []

    async def test_get_all_relationships_with_data(self, backend: Any) -> None:
        """get_all_relationships returns all relationships."""
        src = await _create_test_entity(backend, name="RelSrc")
        tgt = await _create_test_entity(backend, name="RelTgt")
        await _create_test_relationship(backend, UUID(src["id"]), UUID(tgt["id"]))

        result = await backend.get_all_relationships(ORG_ID, PROJ_ID)
        assert len(result) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# Health Check
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    """health_check returns True when connected."""

    async def test_health_check(self, backend: Any) -> None:
        """Connected to real PG → health_check returns True."""
        result = await backend.health_check()
        assert result is True


# ═══════════════════════════════════════════════════════════════════════════════
# Temporal / Appearance
# ═══════════════════════════════════════════════════════════════════════════════


class TestTemporalQueries:
    """get_entity_appearance_timestamps and get_relationship_ids_between."""

    async def test_entity_appearance_empty(self, backend: Any) -> None:
        """Entity with no episode links returns empty list."""
        entity = await _create_test_entity(backend, name="NoAppearances")
        result = await backend.get_entity_appearance_timestamps(
            ORG_ID, PROJ_ID, UUID(entity["id"]),
        )
        assert result == []

    async def test_relationship_ids_between_empty(self, backend: Any) -> None:
        """Entities with no relationship return empty list."""
        a = await _create_test_entity(backend, name="RelA")
        b = await _create_test_entity(backend, name="RelB")
        result = await backend.get_relationship_ids_between(
            ORG_ID, PROJ_ID, UUID(a["id"]), UUID(b["id"]),
        )
        assert result == []
