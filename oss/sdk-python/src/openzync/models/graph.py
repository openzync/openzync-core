"""Pydantic models for the graph (entity, edge, community) domain."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class GraphNode(BaseModel):
    """A single entity node in the knowledge graph."""

    id: str = Field(..., description="UUID of the entity node.")
    name: str = Field(..., description="Human-readable display name.")
    type: str = Field(..., description="Entity type label.")
    summary: str = Field(default="", description="Text summary or description.")
    created_at: str | None = Field(default=None, description="ISO-8601 creation timestamp.")
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """A single relationship edge in the knowledge graph."""

    id: str = Field(..., description="UUID of the edge.")
    source_id: str = Field(..., description="UUID of the source entity.")
    target_id: str = Field(..., description="UUID of the target entity.")
    type: str = Field(..., description="Relationship label.")
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = Field(default=None)


class GraphNodeDetail(BaseModel):
    """A single entity node with all its incident edges."""

    node: GraphNode = Field(..., description="The entity node.")
    edges: list[GraphEdge] = Field(..., description="Incident edges.")


class GraphCommunity(BaseModel):
    """A community summary node."""

    id: str = Field(..., description="UUID of the community node.")
    name: str = Field(..., description="Community cluster name.")
    summary: str = Field(default="", description="Community summary.")
    member_count: int = Field(default=0, ge=0)
    created_at: str | None = Field(default=None)


class PaginatedGraphNodes(BaseModel):
    """Cursor-paginated response for entity node listing."""

    items: list[GraphNode] = Field(..., description="Entity nodes for this page.")
    next_cursor: str | None = Field(default=None, description="Cursor for the next page.")
    has_more: bool = Field(default=False, description="Whether more pages exist.")


class PaginatedGraphEdges(BaseModel):
    """Cursor-paginated response for edge listing."""

    items: list[GraphEdge] = Field(..., description="Edges for this page.")
    next_cursor: str | None = Field(default=None, description="Cursor for the next page.")
    has_more: bool = Field(default=False, description="Whether more pages exist.")
