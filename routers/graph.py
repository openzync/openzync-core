"""Graph query endpoints — HTTP adapter layer only.

Provides:
- ``GET    /v1/projects/{project_id}/graph/nodes``              — List entity nodes
- ``GET    /v1/projects/{project_id}/graph/nodes/{node_id}``    — Get single node with edges
- ``DELETE /v1/projects/{project_id}/graph/nodes/{node_id}``    — Delete entity node
- ``GET    /v1/projects/{project_id}/graph/edges``              — List relationship edges
- ``GET    /v1/projects/{project_id}/graph/communities``        — List community summaries

Every endpoint is guarded by ``require_project_membership`` for unified
authentication and project authorization, and ``project_id`` is passed to
the graph service layer for backend-level project isolation.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core.exceptions import NotFoundError
from dependencies.project_auth import require_project_membership
from dependencies.services import get_graph_service
from schemas.graph import (
    GraphCommunitiesListResponse,
    GraphCommunity,
    GraphEdge,
    GraphEdgesListResponse,
    GraphNode,
    GraphNodeDetail,
    GraphNodeDetailResponse,
    GraphNodesListResponse,
    PaginatedGraphEdges,
    PaginatedGraphNodes,
)
from services.graph_service import GraphService

router = APIRouter(
    prefix="/v1/projects/{project_id}/graph",
    tags=["Knowledge Graph"],
)


# ── GET /nodes — List entity nodes ──────────────────────────────────────────────


@router.get(
    "/nodes",
    response_model=GraphNodesListResponse,
    dependencies=[Depends(require_project_membership)],
    summary="List entity nodes",
    description="List entity nodes in the project's knowledge graph with "
    "optional type filtering and cursor-based pagination.",
    responses={
        200: {"description": "Paginated list of entity nodes."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
    },
)
async def list_graph_nodes(
    request: Request,
    entity_type: str | None = Query(
        default=None,
        description="Optional filter by entity type (e.g. 'Person', 'Organization').",
    ),
    session_id: UUID | None = Query(
        default=None,
        description="Optional: scope entities to those linked to a specific session.",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum results per page (1–200).",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque cursor for pagination from a previous response.",
    ),
    service: GraphService = Depends(get_graph_service),
) -> GraphNodesListResponse:
    """List entity nodes with optional type filter and cursor pagination."""
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])

    result = await service.get_entities(
        org_id=org_id,
        project_id=project_id,
        entity_type=entity_type,
        session_id=session_id,
        limit=limit,
        cursor=cursor,
    )

    return GraphNodesListResponse(
        data=PaginatedGraphNodes(
            items=[GraphNode(**item) for item in result["items"]],
            next_cursor=result.get("next_cursor"),
            has_more=result.get("has_more", False),
        ),
    )


# ── GET /nodes/{node_id} — Get single node with edges ──────────────────────────


@router.get(
    "/nodes/{node_id}",
    response_model=GraphNodeDetailResponse,
    dependencies=[Depends(require_project_membership)],
    summary="Get entity node with incident edges",
    description="Retrieve a single entity node and all its incident "
    "edges from the knowledge graph.",
    responses={
        200: {"description": "Entity node with incident edges."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        404: {"description": "Entity not found."},
    },
)
async def get_graph_node(
    request: Request,
    node_id: UUID,
    service: GraphService = Depends(get_graph_service),
) -> GraphNodeDetailResponse:
    """Get a single entity node with all its incident edges."""
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])

    result = await service.get_entity(
        org_id=org_id,
        project_id=project_id,
        entity_id=node_id,
    )

    return GraphNodeDetailResponse(
        data=GraphNodeDetail(
            node=GraphNode(**result["node"]),
            edges=[GraphEdge(**edge) for edge in result["edges"]],
        ),
    )


# ── DELETE /nodes/{node_id} — Delete entity node ───────────────────────────────


@router.delete(
    "/nodes/{node_id}",
    status_code=204,
    dependencies=[Depends(require_project_membership)],
    summary="Delete entity node",
    description="Delete an entity node and all its incident edges "
    "from the knowledge graph.",
    responses={
        204: {"description": "Entity deleted successfully (no content)."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        404: {"description": "Entity not found."},
    },
)
async def delete_graph_node(
    request: Request,
    node_id: UUID,
    service: GraphService = Depends(get_graph_service),
) -> None:
    """Delete an entity node from the knowledge graph."""
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])

    deleted = await service.delete_entity(
        org_id=org_id,
        project_id=project_id,
        entity_id=node_id,
    )
    if not deleted:
        raise NotFoundError(
            message=f"Entity {node_id} not found in the knowledge graph.",
            detail={"entity_id": str(node_id), "org_id": str(org_id)},
        )


# ── GET /edges — List relationship edges ────────────────────────────────────────


@router.get(
    "/edges",
    response_model=GraphEdgesListResponse,
    dependencies=[Depends(require_project_membership)],
    summary="List relationship edges",
    description="List relationship edges for a specific entity with "
    "optional predicate filtering.",
    responses={
        200: {"description": "Paginated list of relationship edges."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        422: {"description": "Missing required subject_id parameter."},
    },
)
async def list_graph_edges(
    request: Request,
    subject_id: UUID | None = Query(
        default=None,
        description="Required: UUID of the source entity whose edges to list.",
    ),
    predicate: str | None = Query(
        default=None,
        description="Optional filter by edge label (e.g. 'works_at', 'mentions').",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum results per page (1–200).",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque cursor for pagination from a previous response.",
    ),
    service: GraphService = Depends(get_graph_service),
) -> GraphEdgesListResponse:
    """List relationship edges with optional predicate filtering."""
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])

    if subject_id is None:
        raise HTTPException(
            status_code=422,
            detail="The 'subject_id' query parameter is required to list edges.",
        )

    result = await service.get_edges(
        org_id=org_id,
        project_id=project_id,
        subject_id=subject_id,
        predicate=predicate,
        limit=limit,
        cursor=cursor,
    )

    return GraphEdgesListResponse(
        data=PaginatedGraphEdges(
            items=[GraphEdge(**item) for item in result["items"]],
            next_cursor=result.get("next_cursor"),
            has_more=result.get("has_more", False),
        ),
    )


# ── GET /communities — List community summaries ────────────────────────────────


@router.get(
    "/communities",
    response_model=GraphCommunitiesListResponse,
    dependencies=[Depends(require_project_membership)],
    summary="List community summaries",
    description="List community summary nodes for the project's knowledge "
    "graph.  Community detection runs as a scheduled background task — "
    "this endpoint returns an empty list until communities are computed.",
    responses={
        200: {"description": "List of community summary nodes."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
    },
)
async def list_communities(
    request: Request,
    service: GraphService = Depends(get_graph_service),
) -> GraphCommunitiesListResponse:
    """List community summary nodes."""
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])

    communities = await service.get_communities(
        org_id=org_id,
        project_id=project_id,
    )

    return GraphCommunitiesListResponse(
        data=[GraphCommunity(**c) for c in communities],
    )
