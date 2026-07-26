"""Observation query service — read-only retrieval of persisted observations.

Observations are computed by the ``compute_observations`` background worker
and stored via the graph backend.  This service provides the read path only
— no create, update, or delete operations.
"""

from __future__ import annotations

from uuid import UUID

from packages.graph_backend.interface import GraphBackend
from schemas.observation import ObservationListResponse, ObservationResponse


class ObservationQueryService:
    """Read-only service for retrieving observations via the graph backend.

    Wraps ``GraphBackend.get_observations()`` with schema conversion to
    ``ObservationListResponse``.

    Args:
        graph_backend: A resolved ``GraphBackend`` instance for the
            current org/project.
    """

    def __init__(self, graph_backend: GraphBackend) -> None:
        self._graph_backend = graph_backend

    async def get_observations(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        subject_entity_id: UUID | None = None,
        observation_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ObservationListResponse:
        """List observations for a project with optional filters.

        Args:
            org_id: Organisational scope for RLS enforcement.
            project_id: Project scope for isolation.
            subject_entity_id: Optional — only observations about this entity.
            observation_type: Optional — only observations of this type
                (e.g. ``co_occurrence``, ``temporal_pattern``,
                ``behavioral_pattern``).
            limit: Maximum number of results per page (default 50, max 200).
            cursor: Opaque cursor for cursor-based pagination.

        Returns:
            An ``ObservationListResponse`` with the current page of
            observations.

        Raises:
            GraphBackendUnavailableError: If the graph backend is unreachable.
        """
        result = await self._graph_backend.get_observations(
            org_id=org_id,
            project_id=project_id,
            subject_entity_id=subject_entity_id,
            observation_type=observation_type,
            limit=limit,
            cursor=cursor,
        )
        items = [
            ObservationResponse.model_validate(item) for item in result["items"]
        ]
        return ObservationListResponse(data=items, total=len(items))
