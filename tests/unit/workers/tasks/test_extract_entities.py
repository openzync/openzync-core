"""Unit tests for the entity-extraction post-processing helper.

The standalone ``extract_entities`` ARQ task was retired in favour of the
combined ``enrich_episode`` worker (which calls ``process_entities_output``
as its entities section).  These tests exercise the helper directly —
pronoun filtering, type validation, upsert + link + enrichment bit.  The
caller-owned orchestration (LLM call, idempotency, episode not found,
graph-backend-unavailable propagation) is covered by ``test_enrich_episode.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from schemas.llm_outputs import EntityExtractionOutput
from workers.tasks.extract_entities import process_entities_output

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_ENT_1_ID = str(uuid4())


@pytest.mark.unit
class TestProcessEntitiesOutput:
    """process_entities_output filtering and persistence."""

    @pytest.fixture
    def graph_backend(self) -> MagicMock:
        gb = MagicMock()
        gb.link_entity_to_episode = AsyncMock()
        return gb

    @pytest.fixture
    def entity_repo(self) -> MagicMock:
        repo = MagicMock()
        repo.upsert_entity = AsyncMock(return_value={"id": _ENT_1_ID})
        repo.upsert_relationship = AsyncMock(return_value={"id": str(uuid4())})
        return repo

    @pytest.fixture
    def episode_repo(self) -> MagicMock:
        repo = MagicMock()
        repo.apply_enrichment_bits = AsyncMock()
        return repo

    @pytest.fixture
    def db(self) -> AsyncMock:
        m = AsyncMock()
        m.flush = AsyncMock()
        return m

    def _parsed(self, entities: list[dict], relationships: list[dict] | None = None) -> EntityExtractionOutput:
        return EntityExtractionOutput(
            entities=entities,
            relationships=relationships or [],
        )

    @pytest.mark.asyncio
    async def test_entities_upserted_linked_and_bits_set(
        self,
        db: AsyncMock,
        graph_backend: MagicMock,
        entity_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """Happy path: upsert, link to episode, enrichment bit + flush."""
        parsed = self._parsed(
            entities=[
                {"name": "Alice", "type": "Person", "summary": "Alice (Person)"},
                {"name": "Bob", "type": "Person", "summary": "Bob (Person)"},
            ],
            relationships=[
                {"subject": "Alice", "predicate": "knows", "object": "Bob"},
            ],
        )

        name_map = await process_entities_output(
            db=db,
            graph_backend=graph_backend,
            entity_repo=entity_repo,
            episode_repo=episode_repo,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=parsed,
            entity_types=["Person"],
        )

        assert entity_repo.upsert_entity.await_count == 2
        assert entity_repo.upsert_relationship.await_count == 1
        assert graph_backend.link_entity_to_episode.await_count == 2
        episode_repo.apply_enrichment_bits.assert_awaited_once_with(
            UUID(_EPISODE_ID),
            __import__("workers.tasks.base", fromlist=["ENRICHMENT_ENTITIES"]).ENRICHMENT_ENTITIES,
        )
        db.flush.assert_awaited_once()
        # Returned map keys both names → their UUIDs
        assert set(name_map) == {"Alice", "Bob"}
        assert name_map["Alice"] == _ENT_1_ID

    @pytest.mark.asyncio
    async def test_pronouns_filtered(
        self,
        db: AsyncMock,
        graph_backend: MagicMock,
        entity_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """Pronoun-like entities are filtered out, real ones persisted."""
        parsed = self._parsed(
            entities=[
                {"name": "Alice", "type": "Person", "summary": "Alice (Person)"},
                {"name": "I", "type": "pronoun", "summary": "I (pronoun)"},
            ],
        )

        await process_entities_output(
            db=db,
            graph_backend=graph_backend,
            entity_repo=entity_repo,
            episode_repo=episode_repo,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=parsed,
            entity_types=["Person"],
        )

        assert entity_repo.upsert_entity.await_count == 1
        name = entity_repo.upsert_entity.await_args.kwargs["name"]
        assert name == "Alice"

    @pytest.mark.asyncio
    async def test_pronoun_relationship_skipped(
        self,
        db: AsyncMock,
        graph_backend: MagicMock,
        entity_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """Relationships referencing pronouns are not persisted."""
        parsed = self._parsed(
            entities=[
                {"name": "Alice", "type": "Person", "summary": "Alice (Person)"},
            ],
            relationships=[
                {"subject": "I", "predicate": "works_with", "object": "Alice"},
            ],
        )

        await process_entities_output(
            db=db,
            graph_backend=graph_backend,
            entity_repo=entity_repo,
            episode_repo=episode_repo,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=parsed,
            entity_types=["Person"],
        )

        assert entity_repo.upsert_relationship.await_count == 0

    @pytest.mark.asyncio
    async def test_empty_entities_no_upsert(
        self,
        db: AsyncMock,
        graph_backend: MagicMock,
        entity_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """No entities extracted → no upserts, bit still set."""
        parsed = self._parsed(entities=[])

        result = await process_entities_output(
            db=db,
            graph_backend=graph_backend,
            entity_repo=entity_repo,
            episode_repo=episode_repo,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=parsed,
            entity_types=["Person"],
        )

        assert result == {}
        entity_repo.upsert_entity.assert_not_awaited()
        episode_repo.apply_enrichment_bits.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_entity_without_name_skipped(
        self,
        db: AsyncMock,
        graph_backend: MagicMock,
        entity_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """Entities with blank names are dropped before upsert."""
        parsed = self._parsed(
            entities=[
                {"name": "  ", "type": "Person", "summary": None},
                {"name": "Alice", "type": "Person", "summary": None},
            ],
        )

        await process_entities_output(
            db=db,
            graph_backend=graph_backend,
            entity_repo=entity_repo,
            episode_repo=episode_repo,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=parsed,
            entity_types=["Person"],
        )

        assert entity_repo.upsert_entity.await_count == 1
        assert entity_repo.upsert_entity.await_args.kwargs["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_invalid_type_reassigned_to_custom(
        self,
        db: AsyncMock,
        graph_backend: MagicMock,
        entity_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """Entity types outside the allowed ontology become ``Custom``."""
        parsed = self._parsed(
            entities=[
                {"name": "Widget", "type": "NotInOntology", "summary": None},
            ],
        )

        await process_entities_output(
            db=db,
            graph_backend=graph_backend,
            entity_repo=entity_repo,
            episode_repo=episode_repo,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=parsed,
            entity_types=["Person"],
        )

        kwargs = entity_repo.upsert_entity.await_args.kwargs
        assert kwargs["entity_type"] == "Custom"

    @pytest.mark.asyncio
    async def test_relationship_entity_recovery(
        self,
        db: AsyncMock,
        graph_backend: MagicMock,
        entity_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """Relationship names not declared as entities are auto-created."""
        parsed = self._parsed(
            entities=[],
            relationships=[
                {"subject": "Alice", "predicate": "works_at", "object": "Acme Corp"},
            ],
        )

        await process_entities_output(
            db=db,
            graph_backend=graph_backend,
            entity_repo=entity_repo,
            episode_repo=episode_repo,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=parsed,
            entity_types=["Person"],
        )

        # Both relationship endpoints auto-created as Custom entities,
        # then the relationship is upserted.
        assert entity_repo.upsert_entity.await_count == 2
        assert entity_repo.upsert_relationship.await_count == 1
