"""Abstract interface for graph-database operations.

The ``GraphBackend`` ABC defines the contract every graph backend must
satisfy.  The shipped implementation is:

- :class:`PostgresGraphBackend` — PostgreSQL-native

Every method requires ``org_id`` and ``project_id`` — OpenZep enforces
strict organisational and project-level isolation.  No cross-project
graph traversal is possible.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID


class GraphBackend(ABC):
    """Abstract interface for graph database operations.

    Every data-access method accepts ``org_id`` (tenant isolation) and
    ``project_id`` (project-level scoping).
    """

    # ── Entity CRUD ────────────────────────────────────────────────────────────

    @abstractmethod
    async def create_entity(
        self,
        org_id: UUID,
        project_id: UUID,
        name: str,
        entity_type: str,
        summary: str | None = None,
    ) -> dict:
        """Create a new entity node in the graph.

        Args:
            org_id: Organisational scope — the entity belongs to this org.
            project_id: Project scope — the entity belongs to this project.
            name: Human-readable name for the entity.
            entity_type: Type label (e.g. ``"person"``, ``"document"``,
                ``"topic"``).
            summary: Optional text summary or description.

        Returns:
            A dictionary representing the created entity with at minimum
            ``id``, ``name``, ``type``, and ``created_at`` keys.
        """
        ...

    @abstractmethod
    async def get_entity(
        self, org_id: UUID, project_id: UUID, entity_id: UUID
    ) -> dict | None:
        """Retrieve an entity node by its ID.

        Args:
            org_id: Organisational scope for isolation.
            project_id: Project scope for isolation.
            entity_id: The UUID of the entity to fetch.

        Returns:
            The entity dict, or ``None`` if no entity with that ID exists
            within the given org and project.
        """
        ...

    @abstractmethod
    async def delete_entity(
        self, org_id: UUID, project_id: UUID, entity_id: UUID
    ) -> bool:
        """Remove an entity node from the graph.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            entity_id: The UUID of the entity to delete.

        Returns:
            ``True`` if the entity was deleted, ``False`` if it did not exist.
        """
        ...

    # ── Relationships ──────────────────────────────────────────────────────────

    @abstractmethod
    async def create_relationship(
        self,
        org_id: UUID,
        project_id: UUID,
        source_id: UUID,
        target_id: UUID,
        relationship_type: str,
        properties: dict | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> dict:
        """Create a directed edge between two entity nodes.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            source_id: UUID of the source entity.
            target_id: UUID of the target entity.
            relationship_type: Label for the edge (e.g. ``"mentions"``,
                ``"authored_by"``).
            properties: Optional key-value metadata attached to the edge.
            valid_from: Optional temporal validity start (ISO-8601).
            valid_to: Optional temporal validity end (ISO-8601).

        Returns:
            A dictionary representing the created relationship with at
            minimum ``id``, ``source_id``, ``target_id``, ``type``, and
            ``created_at`` keys.
        """
        ...

    # ── Traversal & Search ─────────────────────────────────────────────────────

    @abstractmethod
    async def traverse(
        self,
        org_id: UUID,
        project_id: UUID,
        start_node_id: UUID,
        max_depth: int = 2,
        edge_types: list[str] | None = None,
    ) -> list[dict]:
        """Traverse the graph outward from a starting node.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            start_node_id: UUID of the node to begin traversal from.
            max_depth: Maximum number of edge hops (default 2).
            edge_types: Optional filter — only follow edges with these labels.
                ``None`` means all edge types are followed.

        Returns:
            A list of node dicts reachable within the depth limit, including
            the start node at depth 0.  Each dict includes a ``depth`` key
            indicating the number of hops from the start node.
        """
        ...

    @abstractmethod
    async def search_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        query: str,
        types: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Search entity nodes by name or summary text.

        The backend may use full-text search, fuzzy matching, or vector
        similarity depending on the engine capabilities.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            query: Free-text search string.
            types: Optional filter — only return entities matching these
                type labels.
            limit: Maximum number of results to return.
            offset: Number of results to skip (for pagination).

        Returns:
            A list of matching entity dicts, ordered by relevance
            (descending).  Each dict includes a ``score`` key.
        """
        ...

    # ── Entity Listing ──────────────────────────────────────────────────────────

    @abstractmethod
    async def list_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        entity_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List entity nodes with optional type filter and cursor pagination.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            entity_type: Optional filter by entity type (e.g. ``"Person"``).
            limit: Maximum results per page (max 200).
            cursor: Opaque cursor for cursor-based pagination.

        Returns:
            A dict with ``items`` (list of entity dicts), ``next_cursor``
            (str or None), and ``has_more`` (bool).
        """
        ...

    @abstractmethod
    async def list_entity_edges(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
        *,
        predicate: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List all edges incident to a specific entity node.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            entity_id: UUID of the entity node whose edges to list.
            predicate: Optional filter by edge label.
            limit: Maximum results per page (max 200).
            cursor: Opaque cursor for cursor-based pagination.

        Returns:
            A dict with ``items`` (list of edge dicts), ``next_cursor``
            (str or None), and ``has_more`` (bool).
        """
        ...

    @abstractmethod
    async def get_entity_with_edges(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
    ) -> dict | None:
        """Retrieve a single entity node with all its incident edges.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            entity_id: UUID of the entity to fetch.

        Returns:
            A dict with ``node`` (entity dict) and ``edges`` (list of edge
            dicts), or ``None`` if the entity does not exist.
        """
        ...

    # ── Observability ──────────────────────────────────────────────────────────

    @abstractmethod
    async def health_check(self) -> bool:
        """Verify the graph backend is reachable and responsive.

        Returns:
            ``True`` if the backend is healthy, ``False`` otherwise.
        """
        ...
