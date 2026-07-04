"""LangChain tools for OpenZync knowledge graph operations.

Provides tools that give LLM agents read access to the OpenZync knowledge
graph — searching, listing nodes, and retrieving node details.
"""

from __future__ import annotations

import asyncio
from typing import Any, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from openzync.client import AsyncOpenZync

# ── Input schemas ───────────────────────────────────────────────────────────


class GraphSearchInput(BaseModel):
    """Input schema for graph search."""

    query: str = Field(..., description="Natural-language search query.")
    project_id: str = Field(..., description="OpenZync project UUID to search within.")
    types: str | None = Field(
        default=None,
        description="Comma-separated result types: episodes, facts, entities.",
    )
    limit: int | None = Field(default=None, description="Maximum results (default 20).")


class GraphNodeDetailInput(BaseModel):
    """Input schema for node detail lookup."""

    project_id: str = Field(..., description="OpenZync project UUID.")
    node_id: str = Field(..., description="UUID of the node to retrieve.")


class ListGraphNodesInput(BaseModel):
    """Input schema for listing graph nodes."""

    project_id: str = Field(..., description="OpenZync project UUID.")
    entity_type: str | None = Field(
        default=None,
        description="Optional entity type filter.",
    )
    limit: int | None = Field(default=None, description="Maximum nodes (default 50).")


# ── Tools ───────────────────────────────────────────────────────────────────


class GraphSearchTool(BaseTool):
    """Tool that searches the OpenZync knowledge graph.

    Agents can use this to retrieve contextually relevant episodes, facts,
    or entities from a project's persistent memory.
    """

    name: str = "graph_search"
    description: str = (
        "Search the project's knowledge graph for contextually relevant "
        "episodes, facts, or entities. Use this to recall past "
        "conversations, user preferences, or business data."
    )
    args_schema: Type[BaseModel] = GraphSearchInput
    client: AsyncOpenZync

    def _run(
        self,
        query: str,
        project_id: str,
        types: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Execute the search (sync).

        Args:
            query: Search query.
            project_id: OpenZync project UUID.
            types: Result type filter.
            limit: Max results.

        Returns:
            Formatted search results string.
        """
        return _run_async(
            self._arun(query, project_id=project_id, types=types, limit=limit)
        )

    async def _arun(
        self,
        query: str,
        project_id: str,
        types: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Execute the search (async)."""
        results = await self.client.graph.search(
            project_id,
            query,
            types=types or "episodes,facts",
            limit=limit or 20,
        )
        if not results:
            return "No results found."

        lines: list[str] = []
        for i, r in enumerate(results, 1):
            content = r.get("content", "") or r.get("name", "")
            score = r.get("score", 0.0)
            rtype = r.get("type", "unknown")
            lines.append(f"{i}. [{rtype}] (score: {score:.3f}) {content}")

        return "\n".join(lines)


class GraphNodeDetailTool(BaseTool):
    """Tool that retrieves detailed information about a graph node."""

    name: str = "graph_node_detail"
    description: str = (
        "Get detailed information about a specific entity node in the "
        "knowledge graph, including its summary and all incident relationships."
    )
    args_schema: Type[BaseModel] = GraphNodeDetailInput
    client: AsyncOpenZync

    def _run(self, project_id: str, node_id: str) -> str:
        """Retrieve node details (sync)."""
        return _run_async(self._arun(project_id=project_id, node_id=node_id))

    async def _arun(self, project_id: str, node_id: str) -> str:
        """Retrieve node details (async)."""
        detail = await self.client.graph.node_detail(project_id, node_id)
        lines: list[str] = [
            f"Node: {detail.node.name} ({detail.node.type})",
            f"Summary: {detail.node.summary}",
            "",
            "Relationships:",
        ]
        for edge in detail.edges:
            lines.append(
                f"  - {edge.source_id} --[{edge.type}]--> {edge.target_id}"
            )
        return "\n".join(lines)


class ListGraphNodesTool(BaseTool):
    """Tool that lists entity nodes in the knowledge graph."""

    name: str = "list_graph_nodes"
    description: str = (
        "List entity nodes in the project's knowledge graph, optionally "
        "filtered by entity type."
    )
    args_schema: Type[BaseModel] = ListGraphNodesInput
    client: AsyncOpenZync

    def _run(
        self,
        project_id: str,
        entity_type: str | None = None,
        limit: int | None = None,
    ) -> str:
        """List nodes (sync)."""
        return _run_async(
            self._arun(project_id=project_id, entity_type=entity_type, limit=limit)
        )

    async def _arun(
        self,
        project_id: str,
        entity_type: str | None = None,
        limit: int | None = None,
    ) -> str:
        """List nodes (async)."""
        paginator = await self.client.graph.nodes(
            project_id,
            entity_type=entity_type,
            limit=limit or 50,
        )
        nodes: list[str] = []
        async for node in paginator:
            nodes.append(f"- {node.name} ({node.type})")
        if not nodes:
            return "No nodes found."
        return "\n".join(nodes)


def _run_async(coro: Any) -> Any:
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)
