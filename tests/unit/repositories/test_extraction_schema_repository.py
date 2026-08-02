"""Unit tests for ExtractionSchemaRepository — schema CRUD and queries."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.extraction_schema_repository import ExtractionSchemaRepository


pytestmark = pytest.mark.unit


class TestExtractionSchemaRepository:
    """ExtractionSchemaRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    SCHEMA_ID = UUID("00000000-0000-0000-0000-000000000010")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> ExtractionSchemaRepository:
        return ExtractionSchemaRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_schema(self, **overrides: object) -> MagicMock:
        s = MagicMock()
        s.id = overrides.get("id", self.SCHEMA_ID)
        s.organization_id = overrides.get("organization_id", self.ORG_ID)
        s.name = overrides.get("name", "test-schema")
        s.type = overrides.get("type", "structured")
        s.json_schema = overrides.get("json_schema", {"type": "object"})
        s.prompt_template = overrides.get("prompt_template", None)
        s.is_active = overrides.get("is_active", True)
        s.created_at = overrides.get("created_at", None)
        return s

    # ── get_by_id ──────────────────────────────────────────────────────────────

    async def test_get_by_id_found(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns schema when found."""
        schema = self._mock_schema()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = schema
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(org_id=self.ORG_ID, schema_id=self.SCHEMA_ID)

        assert result == schema

    async def test_get_by_id_not_found(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(org_id=self.ORG_ID, schema_id=self.SCHEMA_ID)

        assert result is None

    # ── get_by_name ────────────────────────────────────────────────────────────

    async def test_get_by_name_found(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_name returns schema when name matches."""
        schema = self._mock_schema(name="my-schema")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = schema
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_name(org_id=self.ORG_ID, name="my-schema")

        assert result == schema

    async def test_get_by_name_not_found(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_name returns None when name does not exist."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_name(org_id=self.ORG_ID, name="nonexistent")

        assert result is None

    # ── get_all ────────────────────────────────────────────────────────────────

    async def test_get_all(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """get_all returns all schemas for an org."""
        schemas = [self._mock_schema(), self._mock_schema()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = schemas
        mock_db.execute.return_value = mock_result

        result = await repo.get_all(org_id=self.ORG_ID)

        assert result == schemas

    async def test_get_all_filtered_by_type(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """get_all filters by schema_type."""
        schemas = [self._mock_schema(type="classification")]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = schemas
        mock_db.execute.return_value = mock_result

        result = await repo.get_all(
            org_id=self.ORG_ID, schema_type="classification"
        )

        assert result == schemas

    async def test_get_all_filtered_by_active(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """get_all filters by is_active."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.get_all(org_id=self.ORG_ID, is_active=True)

        assert result == []

    async def test_get_all_empty(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """get_all returns empty list when no schemas."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.get_all(org_id=self.ORG_ID)

        assert result == []

    # ── create ─────────────────────────────────────────────────────────────────

    async def test_create(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """create inserts and returns a new schema."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            org_id=self.ORG_ID,
            name="new-schema",
            json_schema={"type": "object", "properties": {}},
            type="structured",
            prompt_template=None,
        )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_create_with_prompt(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """create accepts an optional prompt_template."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            org_id=self.ORG_ID,
            name="with-prompt",
            json_schema={},
            type="classification",
            prompt_template="Classify the intent",
        )

        assert result is not None

    # ── update ─────────────────────────────────────────────────────────────────

    async def test_update(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """update modifies schema fields."""
        schema = self._mock_schema()
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.update(
            schema=schema, name="updated-name", json_schema={"type": "array"}
        )

        assert result is not None
        assert result.name == "updated-name"
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_update_skips_type(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """update ignores changes to the type field."""
        schema = self._mock_schema(type="structured")
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.update(schema=schema, type="classification")

        assert result.type == "structured"  # unchanged

    async def test_update_empty_kwargs(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """update with empty kwargs just flushes."""
        schema = self._mock_schema()
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.update(schema=schema)

        assert result is not None

    # ── soft_delete ────────────────────────────────────────────────────────────

    async def test_soft_delete(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """soft_delete sets is_active to False."""
        schema = self._mock_schema(is_active=True)
        mock_db.flush.return_value = None

        await repo.soft_delete(schema=schema)

        assert schema.is_active is False
        mock_db.flush.assert_awaited_once()

    # ── count_for_org ──────────────────────────────────────────────────────────

    async def test_count_for_org(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """count_for_org returns schema count."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 7
        mock_db.execute.return_value = mock_result

        count = await repo.count_for_org(org_id=self.ORG_ID)

        assert count == 7

    async def test_count_for_org_filtered(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """count_for_org filters by schema_type."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 3
        mock_db.execute.return_value = mock_result

        count = await repo.count_for_org(
            org_id=self.ORG_ID, schema_type="classification"
        )

        assert count == 3

    # ── get_classification_labels ──────────────────────────────────────────────

    async def test_get_classification_labels(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """get_classification_labels returns JSON schemas of active classifications."""
        labels = [{"label": "support"}, {"label": "sales"}]
        mock_result = MagicMock()
        mock_result.all.return_value = [(l,) for l in labels]
        mock_db.execute.return_value = mock_result

        result = await repo.get_classification_labels(org_id=self.ORG_ID)

        assert result == labels

    async def test_get_classification_labels_empty(
        self, repo: ExtractionSchemaRepository, mock_db: AsyncMock
    ) -> None:
        """get_classification_labels returns empty list when none active."""
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.get_classification_labels(org_id=self.ORG_ID)

        assert result == []
