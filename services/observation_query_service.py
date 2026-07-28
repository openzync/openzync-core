"""Observation query service — read-only retrieval of persisted observations.

Observations are computed by the ``compute_observations`` background worker
and stored via the graph backend.  This service provides the read path only
— no create, update, or delete operations.  Entity IDs are resolved to
human-readable names via ``GraphBackend.resolve_entity_names()``.
"""

from __future__ import annotations

from uuid import UUID

from packages.graph_backend.interface import GraphBackend
from schemas.observation import ObservationListResponse, ObservationResponse


class ObservationQueryService:
    """Read-only service for retrieving observations via the graph backend.

    Wraps ``GraphBackend.get_observations()`` with schema conversion to
    ``ObservationListResponse`` and batch-resolves entity ID → name.

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
            observations and resolved entity names.

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

        # ── Batch-resolve entity IDs to human-readable names ──────────────
        entity_ids: set[UUID] = set()
        for item in result["items"]:
            entity_ids.add(UUID(item["subject_entity_id"]))
            if item.get("related_entity_id"):
                entity_ids.add(UUID(item["related_entity_id"]))
        entity_ids.discard(None)  # type: ignore[arg-type]

        name_map: dict[str, dict] = {}
        if entity_ids:
            try:
                name_map = await self._graph_backend.resolve_entity_names(
                    org_id, project_id, list(entity_ids)
                )
            except Exception:
                # Non-critical — observations returned without names
                name_map = {}

        for item in result["items"]:
            item["subject_entity_name"] = (
                name_map.get(str(item["subject_entity_id"]), {}).get("name")
            )
            related_id = item.get("related_entity_id")
            item["related_entity_name"] = (
                name_map.get(str(related_id), {}).get("name")
                if related_id else None
            )

        items = [
            ObservationResponse.model_validate(item) for item in result["items"]
        ]
        return ObservationListResponse(data=items, total=len(items))
