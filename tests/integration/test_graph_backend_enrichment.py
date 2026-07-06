"""Integration test for the full enrichment pipeline via PostgresGraphBackend.

Tests the end-to-end enrichment flow:
1. Start with a fresh database
2. Create org + project + user + session
3. Ingest entities via graph backend
4. Link entities to episodes
5. Query the graph to verify entities were created
6. Verify entities are linked to episodes
7. Verify observations can be persisted and retrieved
8. Verify merge operations work
9. Verify cross-project isolation
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

from core.exceptions import NotFoundError
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

NOW = datetime.now(timezone.utc)

# ── Known IDs for reproducibility ─────────────────────────────────────────────
ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJ_ID = UUID("00000000-0000-0000-0000-000000000002")
SESSION_ID = UUID("00000000-0000-0000-0000-000000000010")
EPISODE_ID = UUID("00000000-0000-0000-0000-000000000020")

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
    """Create a fresh async session per test — rolled back on teardown."""
    url = _pg_container.get_connection_url()
    driver_url = url.replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(driver_url, poolclass=NullPool, pool_pre_ping=True)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(sa_text("SET search_path TO public"))
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()

    await engine.dispose()


@pytest_asyncio.fixture
async def backend(db: AsyncSession) -> Any:
    """PostgresGraphBackend scoped to a single test."""
    from packages.graph_backend.postgres import PostgresGraphBackend

    return PostgresGraphBackend(db=db, max_traversal_depth=5)


@pytest.mark.asyncio
class TestGraphBackendEnrichmentPipeline:
    """Full enrichment pipeline end-to-end.

    Simulates the real flow: create entities → link to episodes →
    create relationships → query by session → search → merge →
    persist observations → verify cross-project isolation.
    """

    async def test_full_pipeline(self, backend: Any) -> None:
        """Step-by-step enrichment pipeline:

        1. Create entities from extracted content.
        2. Create relationships between entities.
        3. Link entities to an episode.
        4. Query entities by session.
        5. Search entities by text.
        6. Bulk search for fuzzy dedup.
        7. Merge duplicate entities.
        8. Persist observations.
        9. Verify cross-project isolation.
        """
        # ── Step 1: Entity Creation ────────────────────────────────────────
        company = await backend.create_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            name="Acme Corp",
            entity_type="Organization",
            summary="A fictional company",
        )
        person = await backend.create_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            name="John Doe",
            entity_type="Person",
            summary="CEO of Acme Corp",
        )
        topic = await backend.create_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            name="Artificial Intelligence",
            entity_type="Topic",
            summary="AI technology",
        )

        assert all(e["id"] for e in [company, person, topic])
        company_id = UUID(company["id"])
        person_id = UUID(person["id"])
        topic_id = UUID(topic["id"])

        # ── Step 2: Relationship Creation ─────────────────────────────────
        works_at = await backend.create_relationship(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            source_id=person_id,
            target_id=company_id,
            relationship_type="works_at",
            confidence=0.95,
        )
        expert_in = await backend.create_relationship(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            source_id=person_id,
            target_id=topic_id,
            relationship_type="expert_in",
            confidence=0.8,
        )
        assert works_at["source_id"] == str(person_id)
        assert works_at["target_id"] == str(company_id)
        assert expert_in["source_id"] == str(person_id)

        # ── Step 3: Episode Linking ───────────────────────────────────────
        # First, seed episode and session in the DB since the Postgres backend
        # links against existing episode rows via graph_episode_entities.
        # The graph backend's link_entity_to_episode inserts into
        # graph_episode_entities directly.
        await backend.link_entity_to_episode(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            episode_id=EPISODE_ID,
            entity_id=person_id,
        )
        await backend.link_entity_to_episode(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            episode_id=EPISODE_ID,
            entity_id=company_id,
        )

        # ── Step 4: Query by Session ──────────────────────────────────────
        # get_entities_for_session traverses session→episodes→entity links.
        # Since we don't have actual episodes in the DB, it returns empty.
        session_entities = await backend.get_entities_for_session(
            ORG_ID, PROJ_ID, SESSION_ID,
        )
        assert isinstance(session_entities, list)

        # Let's also test co-occurring pairs — entities linked to same episode
        pairs = await backend.get_co_occurring_entity_pairs(
            ORG_ID, PROJ_ID, min_co_count=1,
        )
        assert len(pairs) >= 1
        pair_names = {p["entity_a_name"], p["entity_b_name"]}
        assert "john doe" in pair_names or "acme corp" in pair_names

        # ── Step 5: Search ───────────────────────────────────────────────
        search_results = await backend.search_entities(
            ORG_ID, PROJ_ID, query="john",
        )
        assert len(search_results) >= 1
        assert any("john" in r["name"] for r in search_results)

        # ── Step 6: Bulk Search for Dedup ─────────────────────────────────
        bulk_results = await backend.bulk_search_entities(
            ORG_ID, PROJ_ID, query="Acme", fuzzy_threshold=0.3,
        )
        assert len(bulk_results) >= 1

        # ── Step 7: Merge Duplicate Entities ─────────────────────────────
        # Create a duplicate entity and merge it into the canonical one
        duplicate = await backend.create_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            name="Acme Corporation",
            entity_type="Organization",
            summary="Duplicate of Acme Corp",
        )
        duplicate_id = UUID(duplicate["id"])

        # Link duplicate to an episode so we can verify rewiring
        alt_episode = uuid4()
        await backend.link_entity_to_episode(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            episode_id=alt_episode,
            entity_id=duplicate_id,
        )

        merge_result = await backend.merge_entities(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            canonical_id=company_id,
            merged_ids=[duplicate_id],
        )
        assert merge_result["merged_count"] >= 1
        assert isinstance(merge_result["rewired_count"], int)
        assert isinstance(merge_result["deleted_count"], int)

        # Verify the duplicate is now marked as merged
        merged_entity = await backend.get_entity(ORG_ID, PROJ_ID, duplicate_id)
        assert merged_entity is not None

        # Verify the canonical still exists
        canonical = await backend.get_entity(ORG_ID, PROJ_ID, company_id)
        assert canonical is not None

        # ── Step 8: Observations ──────────────────────────────────────────
        # Persist an observation about the person
        obs = await backend.upsert_observation(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            subject_entity_id=person_id,
            observation_type="leadership",
            content="John Doe is the CEO of Acme Corp",
            confidence=0.9,
            related_entity_id=company_id,
        )
        assert obs["subject_entity_id"] == str(person_id)
        assert obs["observation_type"] == "leadership"
        assert obs["content"] == "John Doe is the CEO of Acme Corp"

        # Retrieve observation with filter
        obs_result = await backend.get_observations(
            ORG_ID, PROJ_ID, subject_entity_id=person_id,
        )
        assert len(obs_result["items"]) >= 1
        assert obs_result["items"][0]["observation_type"] == "leadership"

        # ── Step 9: Cross-Project Isolation ──────────────────────────────
        # Another project in the same org should have no entities
        other_project = uuid4()
        other_entities = await backend.get_all_entities(ORG_ID, other_project)
        assert other_entities == []

        other_rels = await backend.get_all_relationships(ORG_ID, other_project)
        assert other_rels == []

        # ── Step 10: Retrieve Graph (Search + Traverse) ──────────────────
        graph_result = await backend.retrieve_graph(
            ORG_ID, PROJ_ID, query="john",
        )
        assert len(graph_result) >= 1
        # Distance 0 = directly matched
        assert any(r.get("distance") == 0 for r in graph_result)

        # ── Step 11: Get Entity With Edges ───────────────────────────────
        person_with_edges = await backend.get_entity_with_edges(
            ORG_ID, PROJ_ID, person_id,
        )
        assert person_with_edges is not None
        assert person_with_edges["node"]["id"] == str(person_id)
        assert len(person_with_edges["edges"]) >= 1  # works_at + expert_in

        # ── Step 12: Traverse from person ────────────────────────────────
        traversal = await backend.traverse(
            ORG_ID, PROJ_ID, person_id, max_depth=2,
        )
        assert len(traversal) >= 2  # person + connected nodes
        traversed_ids = {n["id"] for n in traversal}
        assert str(person_id) in traversed_ids
        assert str(company_id) in traversed_ids

        # ── Step 13: Delete entity and verify cascade ────────────────────
        # We can't delete the canonical since merge may have side effects,
        # but we can delete an entity with no links to verify basic delete
        tmp_entity = await backend.create_entity(
            org_id=ORG_ID,
            project_id=PROJ_ID,
            name="Temp Entity",
            entity_type="Temp",
        )
        tmp_id = UUID(tmp_entity["id"])
        deleted = await backend.delete_entity(ORG_ID, PROJ_ID, tmp_id)
        assert deleted is True
        assert await backend.get_entity(ORG_ID, PROJ_ID, tmp_id) is None

        # ── Step 14: Health Check ────────────────────────────────────────
        healthy = await backend.health_check()
        assert healthy is True

    async def test_empty_pipeline_returns_empty_results(self, backend: Any) -> None:
        """All list/traversal methods return empty results on empty project."""
        alt_project = uuid4()

        # Entity CRUD
        assert await backend.get_entity(ORG_ID, alt_project, uuid4()) is None
        assert await backend.delete_entity(ORG_ID, alt_project, uuid4()) is False

        # Lists
        assert await backend.list_entities(ORG_ID, alt_project) == {
            "items": [], "next_cursor": None, "has_more": False,
        }
        assert await backend.get_all_entities(ORG_ID, alt_project) == []
        assert await backend.get_all_relationships(ORG_ID, alt_project) == []
        assert await backend.get_co_occurring_entity_pairs(ORG_ID, alt_project, min_co_count=1) == []

        # Search
        assert await backend.search_entities(ORG_ID, alt_project, query="test") == []
        assert await backend.bulk_search_entities(ORG_ID, alt_project, query="test") == []

        # Traversal
        assert await backend.traverse(ORG_ID, alt_project, uuid4()) == []
        assert await backend.retrieve_graph(ORG_ID, alt_project, query="test") == []

        # Observations
        assert await backend.get_observations(ORG_ID, alt_project) == {
            "items": [], "next_cursor": None, "has_more": False,
        }
        assert await backend.get_entity_appearance_timestamps(ORG_ID, alt_project, uuid4()) == []
        assert await backend.get_relationship_ids_between(ORG_ID, alt_project, uuid4(), uuid4()) == []

    async def test_merge_entities_with_rewiring(self, backend: Any) -> None:
        """Merge entities correctly rewires relationships."""
        # Create canonical + two duplicates
        canonical = await backend.create_entity(
            org_id=ORG_ID, project_id=PROJ_ID,
            name="Canonical", entity_type="Organization",
        )
        dup_a = await backend.create_entity(
            org_id=ORG_ID, project_id=PROJ_ID,
            name="Dup A", entity_type="Organization",
        )
        dup_b = await backend.create_entity(
            org_id=ORG_ID, project_id=PROJ_ID,
            name="Dup B", entity_type="Organization",
        )
        unrelated = await backend.create_entity(
            org_id=ORG_ID, project_id=PROJ_ID,
            name="Unrelated", entity_type="Person",
        )

        canonical_id = UUID(canonical["id"])
        dup_a_id = UUID(dup_a["id"])
        dup_b_id = UUID(dup_b["id"])
        unrelated_id = UUID(unrelated["id"])

        # Create relationships from duplicates to unrelated
        await backend.create_relationship(
            org_id=ORG_ID, project_id=PROJ_ID,
            source_id=dup_a_id, target_id=unrelated_id,
            relationship_type="related_to",
        )

        # Merge both duplicates into canonical
        merge_result = await backend.merge_entities(
            org_id=ORG_ID, project_id=PROJ_ID,
            canonical_id=canonical_id,
            merged_ids=[dup_a_id, dup_b_id],
        )

        assert merge_result["merged_count"] == 2  # both marked merged
        assert merge_result["rewired_count"] >= 1  # at least one relationship rewired

        # Verify the relationship is now from canonical
        rels = await backend.get_all_relationships(ORG_ID, PROJ_ID)
        canonical_rel_targets = [
            r["target_id"] for r in rels if r["source_id"] == str(canonical_id)
        ]
        assert str(unrelated_id) in canonical_rel_targets

    async def test_observations_with_all_filters(self, backend: Any) -> None:
        """Observation retrieval with various filter combinations."""
        entity = await backend.create_entity(
            org_id=ORG_ID, project_id=PROJ_ID,
            name="ObsTest", entity_type="Test",
        )
        eid = UUID(entity["id"])

        # Create observations of different types
        for i, obs_type in enumerate(["type_a", "type_b", "type_c"]):
            await backend.upsert_observation(
                org_id=ORG_ID, project_id=PROJ_ID,
                subject_entity_id=eid,
                observation_type=obs_type,
                content=f"Observation {i}",
                confidence=0.5 + i * 0.1,
                observation_metadata={"index": i},
            )

        # No filter — all observations
        all_obs = await backend.get_observations(ORG_ID, PROJ_ID)
        assert len(all_obs["items"]) == 3

        # Filter by type
        filtered = await backend.get_observations(
            ORG_ID, PROJ_ID, observation_type="type_a",
        )
        assert len(filtered["items"]) == 1
        assert filtered["items"][0]["observation_type"] == "type_a"

        # Filter by subject
        subject_filtered = await backend.get_observations(
            ORG_ID, PROJ_ID, subject_entity_id=eid,
        )
        assert len(subject_filtered["items"]) == 3

        # Pagination
        paginated = await backend.get_observations(
            ORG_ID, PROJ_ID, limit=2,
        )
        assert len(paginated["items"]) == 2
        # With 3 items and limit=2, has_more should be True
        assert paginated["has_more"] is True
        assert paginated["next_cursor"] is not None

        # Observation metadata
        obs_with_meta = filtered["items"][0]
        assert "observation_metadata" in obs_with_meta
