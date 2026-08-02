"""Unit tests for ObservationQueryService — read-only observation retrieval.

The ``GraphBackend`` dependency is fully mocked.  We test filtering by entity,
type, pagination, entity-name resolution, and empty-result handling.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from schemas.observation import ObservationListResponse, ObservationResponse
from services.observation_query_service import ObservationQueryService


@pytest.mark.unit
class TestObservationQueryService:
    """Unit tests for ``ObservationQueryService`` — observation retrieval."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
    ENTITY_ID = UUID("00000000-0000-0000-0000-000000000010")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(self) -> tuple[ObservationQueryService, AsyncMock]:
        """Create an ObservationQueryService with mocked graph backend."""
        mock_backend = AsyncMock()
        service = ObservationQueryService(graph_backend=mock_backend)
        return service, mock_backend

    @staticmethod
    def _make_observation(
        subject_entity_id: UUID,
        observation_type: str = "co_occurrence",
        related_entity_id: UUID | None = None,
        content: str = "Entities frequently co-appear in sessions.",
    ) -> dict:
        """Build an observation dict as returned by the graph backend."""
        now = datetime.now(timezone.utc)
        return {
            "id": str(uuid4()),
            "organization_id": str(UUID("00000000-0000-0000-0000-000000000001")),
            "project_id": str(UUID("00000000-0000-0000-0000-000000000002")),
            "subject_entity_id": str(subject_entity_id),
            "related_entity_id": str(related_entity_id) if related_entity_id else None,
            "observation_type": observation_type,
            "content": content,
            "supporting_fact_ids": None,
            "supporting_relationship_ids": None,
            "confidence": 0.85,
            "valid_from": None,
            "valid_to": None,
            "observation_metadata": None,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "subject_entity_name": None,
            "related_entity_name": None,
        }

    # ── get_observations ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_observations_by_entity(self) -> None:
        """Filtering by subject_entity_id returns only observations for that entity."""
        service, mock_backend = self._make_service()

        obs = [self._make_observation(subject_entity_id=self.ENTITY_ID)]
        mock_backend.get_observations.return_value = {
            "items": obs,
            "next_cursor": None,
            "has_more": False,
        }
        mock_backend.resolve_entity_names.return_value = {
            str(self.ENTITY_ID): {"name": "Test Entity", "entity_type": "topic"},
        }

        result = await service.get_observations(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            subject_entity_id=self.ENTITY_ID,
        )

        assert isinstance(result, ObservationListResponse)
        assert len(result.data) == 1
        assert result.data[0].subject_entity_id == self.ENTITY_ID
        assert result.data[0].subject_entity_name == "Test Entity"
        mock_backend.get_observations.assert_awaited_once_with(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            subject_entity_id=self.ENTITY_ID,
            observation_type=None,
            limit=50,
            cursor=None,
        )

    @pytest.mark.asyncio
    async def test_get_observations_by_type(self) -> None:
        """Filtering by observation_type returns only matching observations."""
        service, mock_backend = self._make_service()

        obs = [self._make_observation(
            subject_entity_id=self.ENTITY_ID,
            observation_type="temporal_pattern",
        )]
        mock_backend.get_observations.return_value = {
            "items": obs,
            "next_cursor": None,
            "has_more": False,
        }
        mock_backend.resolve_entity_names.return_value = {}

        result = await service.get_observations(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            observation_type="temporal_pattern",
        )

        assert len(result.data) == 1
        assert result.data[0].observation_type == "temporal_pattern"
        mock_backend.get_observations.assert_awaited_once_with(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            subject_entity_id=None,
            observation_type="temporal_pattern",
            limit=50,
            cursor=None,
        )

    @pytest.mark.asyncio
    async def test_get_observations_empty(self) -> None:
        """No matching observations returns an empty list with total=0."""
        service, mock_backend = self._make_service()

        mock_backend.get_observations.return_value = {
            "items": [],
            "next_cursor": None,
            "has_more": False,
        }

        result = await service.get_observations(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
        )

        assert result.data == []
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_get_observations_with_cursor_pagination(self) -> None:
        """Cursor pagination is passed through to the graph backend."""
        service, mock_backend = self._make_service()

        obs = [self._make_observation(subject_entity_id=self.ENTITY_ID)]
        mock_backend.get_observations.return_value = {
            "items": obs,
            "next_cursor": "cursor_abc_123",
            "has_more": True,
        }
        mock_backend.resolve_entity_names.return_value = {}

        result = await service.get_observations(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            limit=10,
            cursor="cursor_abc_123",
        )

        assert len(result.data) == 1
        mock_backend.get_observations.assert_awaited_once_with(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            subject_entity_id=None,
            observation_type=None,
            limit=10,
            cursor="cursor_abc_123",
        )

    @pytest.mark.asyncio
    async def test_get_observations_resolves_entity_names(self) -> None:
        """Entity IDs are batch-resolved and attached to response items."""
        service, mock_backend = self._make_service()

        related_id = uuid4()
        obs = [self._make_observation(
            subject_entity_id=self.ENTITY_ID,
            related_entity_id=related_id,
        )]
        mock_backend.get_observations.return_value = {
            "items": obs,
            "next_cursor": None,
            "has_more": False,
        }
        mock_backend.resolve_entity_names.return_value = {
            str(self.ENTITY_ID): {"name": "Subject Entity", "entity_type": "person"},
            str(related_id): {"name": "Related Entity", "entity_type": "topic"},
        }

        result = await service.get_observations(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
        )

        assert result.data[0].subject_entity_name == "Subject Entity"
        assert result.data[0].related_entity_name == "Related Entity"

    @pytest.mark.asyncio
    async def test_get_observations_entity_name_resolution_fails_gracefully(self) -> None:
        """When entity name resolution fails, observations are returned without names."""
        service, mock_backend = self._make_service()

        obs = [self._make_observation(subject_entity_id=self.ENTITY_ID)]
        mock_backend.get_observations.return_value = {
            "items": obs,
            "next_cursor": None,
            "has_more": False,
        }
        # Resolution raises an exception
        mock_backend.resolve_entity_names.side_effect = RuntimeError("Backend timeout")

        result = await service.get_observations(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
        )

        assert len(result.data) == 1
        # Names should be None since resolution failed
        assert result.data[0].subject_entity_name is None
