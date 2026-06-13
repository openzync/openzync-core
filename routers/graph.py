"""Project-scoped graph query endpoints — /v1/projects/{project_id}/{user_id}/graph.

Provides:
- ``GET    /v1/projects/{project_id}/{user_id}/graph/nodes``         — List entity nodes
- ``GET    /v1/projects/{project_id}/{user_id}/graph/nodes/{node_id}`` — Get node with edges
- ``DELETE /v1/projects/{project_id}/{user_id}/graph/nodes/{node_id}`` — Delete entity node
- ``GET    /v1/projects/{project_id}/{user_id}/graph/edges``          — List relationship edges
- ``GET    /v1/projects/{project_id}/{user_id}/graph/communities``    — List community summaries

Every endpoint enforces project-membership access via ``require_project_access``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import EntityNotFoundError, NotFoundError
from dependencies.auth import require_org_id, require_project_access
from dependencies.db import get_db
from packages.graphiti_client.backends.postgres import PostgresGraphBackend
from repositories.user_repository import UserRepository
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
    prefix="/v1/projects/{project_id}/{user_id}/graph",
    tags=["Knowledge Graph (Project-scoped)"],
)


# ── Dependency ──────────────────────────────────────────────────────────────


async def get_graph_service(
    db: AsyncSession = Depends(get_db),
) -> GraphService:
    """FastAPI dependency that yields an initialised :class:`GraphService`."""
    graph_backend = PostgresGraphBackend(db=db)
    return GraphService(graph_backend=graph_backend)


async def _resolve_user(
    db: AsyncSession,
    org_id: UUID,
    user_id: UUID,
) -> None:
    """Verify the user exists in the organization."""
    user_repo = UserRepository(db)
    user = await user_repo.get_by_uuid(org_id, user_id)
    if user is None:
        raise NotFoundError(
            message=f"User {user_id} not found in organization {org_id}",
            detail={"user_id": str(user_id), "org_id": str(org_id)},
        )


# ── GET /nodes — List entity nodes ──────────────────────────────────────────


@router.get(
    "/nodes",
    response_model=GraphNodesListResponse,
    summary="List entity nodes (project-scoped)",
    description="List entity nodes in the user's knowledge graph, scoped to project.",
)
async def list_graph_nodes(
    project_id: UUID,
    user_id: UUID,
    entity_type: str | None = Query(
        default=None,
        description="Optional filter by entity type (e.g. 'Person', 'Organization').",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum results per page (1-200).",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque cursor for pagination.",
    ),
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_project_access),
    service: GraphService = Depends(get_graph_service),
) -> GraphNodesListResponse:
    """List entity nodes with optional type filter and cursor pagination."""
    org_uuid = UUID(org_id)
    await _resolve_user(db, org_uuid, user_id)

    result = await service.get_entities(
        org_id=org_uuid,
        project_id=project_id,
        entity_type=entity_type,
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


# ── GET /nodes/{node_id} — Get single node with edges ──────────────────────


@router.get(
    "/nodes/{node_id}",
    response_model=GraphNodeDetailResponse,
    summary="Get entity node (project-scoped)",
    description="Retrieve a single entity node and its incident edges within a project.",
)
async def get_graph_node(
    project_id: UUID,
    user_id: UUID,
    node_id: UUID,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_project_access),
    service: GraphService = Depends(get_graph_service),
) -> GraphNodeDetailResponse:
    """Get a single entity node with all its incident edges."""
    org_uuid = UUID(org_id)
    await _resolve_user(db, org_uuid, user_id)

    result = await service.get_entity(
        org_id=org_uuid,
        project_id=project_id,
        entity_id=node_id,
    )

    return GraphNodeDetailResponse(
        data=GraphNodeDetail(
            node=GraphNode(**result["node"]),
            edges=[GraphEdge(**edge) for edge in result["edges"]],
        ),
    )


# ── DELETE /nodes/{node_id} — Delete entity node ───────────────────────────


@router.delete(
    "/nodes/{node_id}",
    status_code=204,
    summary="Delete entity node (project-scoped)",
    description="Delete an entity node and its incident edges within a project.",
)
async def delete_graph_node(
    project_id: UUID,
    user_id: UUID,
    node_id: UUID,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_project_access),
    service: GraphService = Depends(get_graph_service),
) -> None:
    """Delete an entity node from the knowledge graph."""
    org_uuid = UUID(org_id)
    await _resolve_user(db, org_uuid, user_id)

    deleted = await service.delete_entity(
        org_id=org_uuid,
        project_id=project_id,
        entity_id=node_id,
    )
    if not deleted:
        raise EntityNotFoundError(
            message=f"Entity {node_id} not found in the knowledge graph.",
            detail={"entity_id": str(node_id), "org_id": str(org_id)},
        )


# ── GET /edges — List relationship edges ────────────────────────────────────


@router.get(
    "/edges",
    response_model=GraphEdgesListResponse,
    summary="List relationship edges (project-scoped)",
    description="List relationship edges for a specific entity within a project.",
)
async def list_graph_edges(
    project_id: UUID,
    user_id: UUID,
    subject_id: UUID | None = Query(
        default=None,
        description="Required: UUID of the source entity whose edges to list.",
    ),
    predicate: str | None = Query(
        default=None,
        description="Optional filter by edge label.",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum results per page (1-200).",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque cursor for pagination.",
    ),
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_project_access),
    service: GraphService = Depends(get_graph_service),
) -> GraphEdgesListResponse:
    """List relationship edges with optional predicate filtering."""
    org_uuid = UUID(org_id)
    await _resolve_user(db, org_uuid, user_id)

    if subject_id is None:
        raise HTTPException(
            status_code=422,
            detail="The 'subject_id' query parameter is required to list edges.",
        )

    result = await service.get_edges(
        org_id=org_uuid,
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


# ── GET /communities — List community summaries ────────────────────────────


@router.get(
    "/communities",
    response_model=GraphCommunitiesListResponse,
    summary="List community summaries (project-scoped)",
    description="List community summary nodes within a project.",
)
async def list_communities(
    project_id: UUID,
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_project_access),
    service: GraphService = Depends(get_graph_service),
) -> GraphCommunitiesListResponse:
    """List community summary nodes within a project."""
    org_uuid = UUID(org_id)
    await _resolve_user(db, org_uuid, user_id)

    communities = await service.get_communities(
        org_id=org_uuid,
        project_id=project_id,
    )

    return GraphCommunitiesListResponse(
        data=[GraphCommunity(**c) for c in communities],
    )
