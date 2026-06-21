"""Pydantic schemas for the graph (entity, edge, community) domain.

Schemas must never import from ``models/``, ``services/``, or ``routers/``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class GraphNode(BaseModel):
    """A single entity node in the knowledge graph.

    Attributes:
        id: UUID string identifying the entity node.
        name: Human-readable display name.
        type: Entity type label (e.g. ``"Person"``, ``"Organization"``).
        summary: Optional text summary or description.
        created_at: ISO-8601 timestamp of node creation.
        metadata: Additional engine-specific metadata.
    """

    id: str = Field(..., description="UUID of the entity node.")
    name: str = Field(..., description="Human-readable display name.")
    type: str = Field(..., description="Entity type label.")
    summary: str = Field(default="", description="Text summary or description.")
    created_at: str | None = Field(default=None, description="ISO-8601 creation timestamp.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Engine-specific metadata.")


class GraphEdge(BaseModel):
    """A single relationship edge in the knowledge graph.

    Attributes:
        id: UUID string identifying the edge.
        source_id: UUID of the source entity node.
        target_id: UUID of the target entity node.
        type: Relationship label (e.g. ``"works_at"``, ``"mentions"``).
        properties: Key-value metadata attached to the edge.
        created_at: ISO-8601 timestamp of edge creation.
    """

    id: str = Field(..., description="UUID of the edge.")
    source_id: str = Field(..., description="UUID of the source entity.")
    target_id: str = Field(..., description="UUID of the target entity.")
    type: str = Field(..., description="Relationship label.")
    properties: dict[str, Any] = Field(default_factory=dict, description="Edge metadata.")
    created_at: str | None = Field(default=None, description="ISO-8601 creation timestamp.")


class GraphNodeDetail(BaseModel):
    """A single entity node with all its incident edges.

    Attributes:
        node: The entity node.
        edges: All incident edges (both incoming and outgoing).
    """

    node: GraphNode = Field(..., description="The entity node.")
    edges: list[GraphEdge] = Field(..., description="Incident edges.")


class GraphCommunity(BaseModel):
    """A community summary node representing a cluster of related entities.

    Attributes:
        id: UUID string identifying the community node.
        name: Human-readable name for the community cluster.
        summary: LLM-generated summary of the community.
        member_count: Number of entity nodes in this community.
        created_at: ISO-8601 timestamp of community creation.
    """

    id: str = Field(..., description="UUID of the community node.")
    name: str = Field(..., description="Community cluster name.")
    summary: str = Field(default="", description="LLM-generated community summary.")
    member_count: int = Field(default=0, ge=0, description="Number of member entities.")
    created_at: str | None = Field(default=None, description="ISO-8601 creation timestamp.")


class PaginatedGraphNodes(BaseModel):
    """Cursor-paginated response for entity node listing.

    Attributes:
        items: List of entity nodes for the current page.
        next_cursor: Opaque cursor for the next page (null if last page).
        has_more: True if additional pages exist beyond this one.
    """

    items: list[GraphNode] = Field(..., description="Entity nodes for this page.")
    next_cursor: str | None = Field(default=None, description="Cursor for the next page.")
    has_more: bool = Field(default=False, description="Whether more pages exist.")


class PaginatedGraphEdges(BaseModel):
    """Cursor-paginated response for edge listing.

    Attributes:
        items: List of edges for the current page.
        next_cursor: Opaque cursor for the next page (null if last page).
        has_more: True if additional pages exist beyond this one.
    """

    items: list[GraphEdge] = Field(..., description="Edges for this page.")
    next_cursor: str | None = Field(default=None, description="Cursor for the next page.")
    has_more: bool = Field(default=False, description="Whether more pages exist.")


class GraphNodesListResponse(BaseModel):
    """Response wrapper for ``GET /v1/projects/{project_id}/graph/nodes``.

    Attributes:
        data: Paginated entity node results.
    """

    data: PaginatedGraphNodes = Field(..., description="Paginated entity nodes.")


class GraphNodeDetailResponse(BaseModel):
    """Response wrapper for ``GET /v1/projects/{project_id}/graph/nodes/{node_id}``.

    Attributes:
        data: The entity node with its incident edges.
    """

    data: GraphNodeDetail = Field(..., description="Entity node with edges.")


class GraphEdgesListResponse(BaseModel):
    """Response wrapper for ``GET /v1/projects/{project_id}/graph/edges``.

    Attributes:
        data: Paginated edge results.
    """

    data: PaginatedGraphEdges = Field(..., description="Paginated edges.")


class GraphCommunitiesListResponse(BaseModel):
    """Response wrapper for ``GET /v1/projects/{project_id}/graph/communities``.

    Attributes:
        data: List of community summary nodes.
    """

    data: list[GraphCommunity] = Field(..., description="Community summary nodes.")
