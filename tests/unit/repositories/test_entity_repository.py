"""Unit tests for EntityRepository — graph-backed entity operations."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import GraphBackendUnavailableError
from repositories.entity_repository import EntityRepository

pytestmark = pytest.mark.unit


class TestEntityRepository:
    """EntityRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
    ENTITY_ID = UUID("00000000-0000-0000-0000-000000000010")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def mock_backend(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def repo(
        self, mock_db: AsyncMock, mock_backend: AsyncMock
    ) -> EntityRepository:
        return EntityRepository(db=mock_db, graph_backend=mock_backend)

    # ── Constructor ────────────────────────────────────────────────────────────

    def test_init_without_backend_raises(
        self, mock_db: AsyncMock
    ) -> None:
        """EntityRepository raises GraphBackendUnavailableError without backend."""
        with pytest.raises(GraphBackendUnavailableError):
            EntityRepository(db=mock_db, graph_backend=None)

    async def test_upsert_entity_creates_new(
        self, repo: EntityRepository, mock_backend: AsyncMock
    ) -> None:
        """upsert_entity creates a new entity when none exists."""
        mock_backend.search_entities.return_value = []
        mock_backend.create_entity.return_value = {
            "id": str(self.ENTITY_ID),
            "name": "Test Entity",
            "type": "Person",
        }

        result = await repo.upsert_entity(
            user_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            name="Test Entity",
            entity_type="Person",
        )

        assert result["name"] == "Test Entity"
        mock_backend.create_entity.assert_awaited_once()

    async def test_upsert_entity_returns_existing(
        self, repo: EntityRepository, mock_backend: AsyncMock
    ) -> None:
        """upsert_entity returns existing entity via backend upsert."""
        existing = {
            "id": str(self.ENTITY_ID),
            "name": "Test Entity",
            "type": "Person",
        }
        mock_backend.search_entities.return_value = [existing]
        mock_backend.create_entity.return_value = existing

        result = await repo.upsert_entity(
            user_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            name="Test Entity",
            entity_type="Person",
        )

        assert result is not None
        mock_backend.create_entity.assert_awaited_once()

    async def test_upsert_entity_backend_error_raises(
        self, repo: EntityRepository, mock_backend: AsyncMock
    ) -> None:
        """upsert_entity wraps backend errors in GraphBackendUnavailableError."""
        mock_backend.search_entities.return_value = []
        mock_backend.create_entity.side_effect = RuntimeError("DB down")

        with pytest.raises(GraphBackendUnavailableError):
            await repo.upsert_entity(
                user_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                name="Test",
                entity_type="Person",
            )

    # ── get_entity_by_name ─────────────────────────────────────────────────────

    async def test_get_entity_by_name_found(
        self, repo: EntityRepository, mock_backend: AsyncMock
    ) -> None:
        """get_entity_by_name returns matching entity."""
        entities = [{"id": str(self.ENTITY_ID), "name": "Alice", "type": "Person"}]
        mock_backend.search_entities.return_value = entities

        result = await repo.get_entity_by_name(
            user_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            name="Alice",
        )

        assert result == entities[0]

    async def test_get_entity_by_name_not_found(
        self, repo: EntityRepository, mock_backend: AsyncMock
    ) -> None:
        """get_entity_by_name returns None when no match."""
        mock_backend.search_entities.return_value = []

        result = await repo.get_entity_by_name(
            user_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            name="Unknown",
        )

        assert result is None

    async def test_get_entity_by_name_fuzzy_match(
        self, repo: EntityRepository, mock_backend: AsyncMock
    ) -> None:
        """get_entity_by_name uses contains matching for partial names."""
        entities = [
            {"id": str(uuid4()), "name": "Alice Johnson", "type": "Person"},
        ]
        mock_backend.search_entities.return_value = entities

        result = await repo.get_entity_by_name(
            user_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            name="Alice",
        )

        assert result is not None
        assert result["name"] == "Alice Johnson"

    async def test_get_entity_by_name_backend_error(
        self, repo: EntityRepository, mock_backend: AsyncMock
    ) -> None:
        """get_entity_by_name wraps backend errors."""
        mock_backend.search_entities.side_effect = RuntimeError("fail")

        with pytest.raises(GraphBackendUnavailableError):
            await repo.get_entity_by_name(
                user_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                name="Test",
            )

    # ── get_entity_by_id ───────────────────────────────────────────────────────

    async def test_upsert_relationship_creates_edge(
        self, repo: EntityRepository, mock_backend: AsyncMock
    ) -> None:
        """upsert_relationship creates a relationship between two entities."""
        subject = {"id": str(uuid4()), "name": "Alice", "type": "Person"}
        obj = {"id": str(uuid4()), "name": "Bob", "type": "Person"}
        mock_backend.search_entities.side_effect = [[subject], [obj]]
        mock_backend.create_relationship.return_value = {
            "id": str(uuid4()),
            "source_id": subject["id"],
            "target_id": obj["id"],
            "type": "knows",
        }

        result = await repo.upsert_relationship(
            subject="Alice",
            predicate="knows",
            obj="Bob",
            user_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
        )

        assert result is not None
        assert result["type"] == "knows"

    async def test_upsert_relationship_subject_not_found(
        self, repo: EntityRepository, mock_backend: AsyncMock
    ) -> None:
        """upsert_relationship returns None when subject not found."""
        mock_backend.search_entities.return_value = []

        result = await repo.upsert_relationship(
            subject="Unknown",
            predicate="knows",
            obj="Bob",
            user_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
        )

        assert result is None

    async def test_upsert_relationship_object_not_found(
        self, repo: EntityRepository, mock_backend: AsyncMock
    ) -> None:
        """upsert_relationship returns None when object not found."""
        subject = {"id": str(uuid4()), "name": "Alice", "type": "Person"}
        mock_backend.search_entities.side_effect = [[subject], []]

        result = await repo.upsert_relationship(
            subject="Alice",
            predicate="knows",
            obj="Unknown",
            user_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
        )

        assert result is None

    async def test_upsert_relationship_backend_error(
        self, repo: EntityRepository, mock_backend: AsyncMock
    ) -> None:
        """upsert_relationship wraps backend errors."""
        subject = {"id": str(uuid4()), "name": "Alice", "type": "Person"}
        obj = {"id": str(uuid4()), "name": "Bob", "type": "Person"}
        mock_backend.search_entities.side_effect = [[subject], [obj]]
        mock_backend.create_relationship.side_effect = RuntimeError("fail")

        with pytest.raises(GraphBackendUnavailableError):
            await repo.upsert_relationship(
                subject="Alice",
                predicate="knows",
                obj="Bob",
                user_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
            )
