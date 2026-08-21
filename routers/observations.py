"""Observation query endpoints — HTTP adapter layer only.

Endpoints:
    GET /v1/projects/{project_id}/observations
        — List observations with optional filtering by entity or type.

Every endpoint is guarded by ``require_project_membership`` for unified
authentication and project-scoped authorization.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from dependencies.auth import require_permission
from dependencies.project_auth import require_project_membership
from dependencies.services import get_graph_backend_for_project
from packages.graph_backend.interface import GraphBackend
from schemas.observation import ObservationListResponse
from services.observation_query_service import ObservationQueryService

router = APIRouter(
    prefix="/v1/projects/{project_id}/observations",
    tags=["Observations"],
)


def _get_observation_query_service(
    graph_backend: GraphBackend = Depends(get_graph_backend_for_project),
) -> ObservationQueryService:
    """Dependency factory for ``ObservationQueryService``.

    Wires in a project-scoped ``GraphBackend`` resolved from the org
    configuration.
    """
    return ObservationQueryService(graph_backend=graph_backend)


@router.get(
    "",
    response_model=ObservationListResponse,
    dependencies=[
        Depends(require_project_membership),
        Depends(require_permission("project:read")),
    ],
    summary="List observations",
    description=(
        "List observations for a project with optional type and entity "
        "filters.  Observations are read-only snapshots of graph-topology "
        "analysis computed by the background worker."
    ),
)
async def list_observations(
    request: Request,
    subject_entity_id: UUID | None = Query(
        default=None,
        description="Filter by subject entity ID.",
    ),
    observation_type: str | None = Query(
        default=None,
        description=(
            "Filter by observation type "
            "(co_occurrence, temporal_pattern, behavioral_pattern)."
        ),
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum results per page.",
    ),
    service: ObservationQueryService = Depends(_get_observation_query_service),
) -> ObservationListResponse:
    """List observations for the current project.

    The ``project_id`` is extracted from the URL path and the ``org_id``
    from ``request.state.org_id`` (set by the auth middleware).
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])
    return await service.get_observations(
        org_id=org_id,
        project_id=project_id,
        subject_entity_id=subject_entity_id,
        observation_type=observation_type,
        limit=limit,
    )
