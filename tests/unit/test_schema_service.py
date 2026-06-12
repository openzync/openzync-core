"""Unit tests for SchemaService — CRUD with mocked repository."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from core.exceptions import NotFoundError
from schemas.extraction_schemas import (
    CreateExtractionSchemaRequest,
    UpdateExtractionSchemaRequest,
)
from services.schema_service import SchemaService


@pytest.mark.unit
class TestSchemaService:
    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

    def _make_service(self) -> tuple[SchemaService, AsyncMock]:
        mock_repo = AsyncMock()
        service = SchemaService(repo=mock_repo)
        return service, mock_repo

    def _mock_schema(self, **kwargs) -> MagicMock:
        m = MagicMock()
        m.id = kwargs.get("id", uuid4())
        m.name = kwargs.get("name", "test-schema")
        m.json_schema = kwargs.get("json_schema", {"type": "object"})
        m.prompt_template = kwargs.get("prompt_template", None)
        m.is_active = kwargs.get("is_active", True)
        m.organization_id = self.ORG_ID
        m.type = kwargs.get("type", "structured")
        return m

    @pytest.mark.asyncio
    async def test_create_schema(self) -> None:
        service, mock_repo = self._make_service()
        mock_repo.get_by_name.return_value = None
        mock_repo.create.return_value = self._mock_schema(name="my-schema")

        payload = CreateExtractionSchemaRequest(
            name="my-schema",
            json_schema={"type": "object"},
        )
        result = await service.create_schema(org_id=self.ORG_ID, payload=payload)
        assert result.name == "my-schema"

    @pytest.mark.asyncio
    async def test_list_schemas(self) -> None:
        service, mock_repo = self._make_service()
        mock_repo.get_all.return_value = [
            self._mock_schema(name="s1"),
            self._mock_schema(name="s2"),
        ]

        result = await service.list_schemas(self.ORG_ID)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_schema_found(self) -> None:
        service, mock_repo = self._make_service()
        schema_id = uuid4()
        mock_repo.get_by_id.return_value = self._mock_schema(id=schema_id)

        result = await service.get_schema(self.ORG_ID, schema_id)
        assert result.name == "test-schema"

    @pytest.mark.asyncio
    async def test_get_schema_not_found(self) -> None:
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_schema(self.ORG_ID, uuid4())

    @pytest.mark.asyncio
    async def test_update_schema(self) -> None:
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._mock_schema(prompt_template="old")
        mock_repo.update.return_value = self._mock_schema(prompt_template="updated")

        payload = UpdateExtractionSchemaRequest(prompt_template="updated")
        result = await service.update_schema(
            self.ORG_ID, uuid4(), payload=payload,
        )
        assert result.prompt_template == "updated"

    @pytest.mark.asyncio
    async def test_delete_schema(self) -> None:
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._mock_schema()

        await service.delete_schema(self.ORG_ID, uuid4())
        mock_repo.soft_delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_schema_not_found(self) -> None:
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError):
            await service.delete_schema(self.ORG_ID, uuid4())
