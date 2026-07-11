"""Abstract interface for graph-database operations.

The ``GraphBackend`` ABC defines the contract every graph backend must
satisfy.  The shipped implementation is:

- :class:`PostgresGraphBackend` — PostgreSQL-native

Every method requires ``org_id`` and ``project_id`` — OpenZync enforces
strict organisational and project-level isolation.  No cross-project
graph traversal is possible.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
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
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any] | None:
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

    @abstractmethod
    async def update_entity(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
        *,
        name: str | None = None,
        entity_type: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Update an entity's mutable fields.

        Only the provided fields are changed; ``None`` fields are left
        untouched.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            entity_id: UUID of the entity to update.
            name: New name, or ``None`` to leave unchanged.
            entity_type: New type label, or ``None`` to leave unchanged.
            summary: New summary text, or ``None`` to leave unchanged.

        Returns:
            The updated entity dict with at minimum ``id``, ``name``,
            ``entity_type``, ``summary``, and ``updated_at`` keys.

        Raises:
            NotFoundError: If no entity with the given ID exists.
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
        properties: dict[str, Any] | None = None,
        confidence: float | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> dict[str, Any]:
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
    ) -> list[dict[str, Any]]:
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
    ) -> list[dict[str, Any]]:
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
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any] | None:
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

    @abstractmethod
    async def retrieve_graph(
        self,
        org_id: UUID,
        project_id: UUID,
        query: str,
        *,
        match_limit: int = 5,
        max_depth: int = 2,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        """Search entities matching query, then BFS-traverse outward.

        Combines entity text search with graph traversal so each backend
        can use its native strengths (recursive CTE, Cypher, etc.).

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            query: Free-text search string.
            match_limit: Max entities to match before traversal.
            max_depth: Max BFS depth from each matched entity.
            max_results: Max total results to return.

        Returns:
            Entity dicts with id, name, type, summary, and distance keys.
            Distance 0 = directly matched, 1+ = reached via traversal.
            Sorted by distance ascending.
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

    # ── Group A: Entity-Episode Linking ─────────────────────────────────────────

    @abstractmethod
    async def link_entity_to_episode(
        self,
        org_id: UUID,
        project_id: UUID,
        episode_id: UUID,
        entity_id: UUID,
    ) -> None:
        """Record that an entity was extracted from (appears in) a specific episode.

        This is the join-table equivalent — maps many-to-many entity↔episode.
        Must be idempotent (ON CONFLICT DO NOTHING).

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            episode_id: UUID of the episode the entity appears in.
            entity_id: UUID of the entity appearing in the episode.

        Raises:
            NotFoundError: If either the episode or entity does not exist.
        """
        ...

    @abstractmethod
    async def get_entities_for_session(
        self,
        org_id: UUID,
        project_id: UUID,
        session_id: UUID,
    ) -> list[dict[str, Any]]:
        """Return all distinct graph entities linked to episodes in a session.

        Traverses session → episodes → episode_entity_links → entities.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            session_id: UUID of the processing session.

        Returns:
            List of entity dicts with ``id``, ``name``, ``entity_type``,
            ``summary`` keys.
        """
        ...

    @abstractmethod
    async def get_co_occurring_entity_pairs(
        self,
        org_id: UUID,
        project_id: UUID,
        min_co_count: int = 2,
    ) -> list[dict[str, Any]]:
        """Find entity pairs that co-appear in episodes above a threshold.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            min_co_count: Minimum number of co-occurring episodes required.
                Defaults to 2 (pairs appearing in at least 2 episodes).

        Returns:
            List of dicts with ``entity_a_id``, ``entity_a_name``,
            ``entity_b_id``, ``entity_b_name``, ``co_count`` (number of
            episodes both entities appear in), sorted by co_count descending.
        """
        ...

    # ── Group B: Bulk / Merge Operations ────────────────────────────────────────

    @abstractmethod
    async def get_all_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        include_merged: bool = False,
    ) -> list[dict[str, Any]]:
        """Return ALL entities for a project (no pagination — for batch workers).

        WARNING: This is for batch workers (merge dedup, community detection).
        Do NOT expose via API — no limit means it can return millions of rows.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            include_merged: If ``True``, include entities that have been
                soft-deleted via merge. Defaults to ``False``.

        Returns:
            A complete list of entity dicts for the project. Each dict
            includes ``id``, ``name``, ``entity_type``, ``summary``,
            ``is_merged``, and ``created_at``.
        """
        ...

    @abstractmethod
    async def get_all_relationships(
        self,
        org_id: UUID,
        project_id: UUID,
    ) -> list[dict[str, Any]]:
        """Return ALL relationships for a project (no pagination).

        Same warning as :meth:`get_all_entities` — batch use only.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.

        Returns:
            A complete list of relationship dicts for the project. Each dict
            includes ``id``, ``source_id``, ``target_id``,
            ``relationship_type``, ``confidence``, and ``created_at``.
            Only non-expired (``invalid_at IS NULL``) relationships are
            returned.
        """
        ...

    @abstractmethod
    async def bulk_search_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        query: str,
        *,
        fuzzy_threshold: float = 0.3,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Search entities using fuzzy string matching for dedup detection.

        Used by ``merge_duplicate_entities`` worker to find potential
        duplicates.  The backend should use trigram similarity, Levenshtein
        distance, or equivalent fuzzy matching.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            query: Search string to match against entity names.
            fuzzy_threshold: Minimum similarity score (0.0 – 1.0) for a
                result to be included. Defaults to 0.3.
            limit: Maximum number of results to return. Defaults to 50.

        Returns:
            List of entity dicts that exceed the similarity threshold,
            sorted by descending score.  Each dict includes all standard
            entity fields plus a ``score`` key (float, 0.0–1.0).
        """
        ...

    @abstractmethod
    async def merge_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        canonical_id: UUID,
        merged_ids: list[UUID],
    ) -> dict[str, Any]:
        """Merge duplicate entities: rewire all edges to canonical, soft-delete merged.

        STRICT ATOMICITY CONTRACT: Must be all-or-nothing. If any step fails,
        no partial state should remain visible. Backends that cannot provide
        atomicity must raise ``NotImplementedError``.

        Steps the backend must take atomically:

        1. Rewire all relationships targeting any ``merged_id`` →
           ``canonical_id`` (both source and target ends).
        2. Delete duplicate relationships created by rewiring (same
           source, target, *and* type after rewiring).
        3. Set ``is_merged = true`` on all ``merged_ids``.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            canonical_id: UUID of the entity that survives the merge.
            merged_ids: UUIDs of entities being absorbed into the
                canonical entity.

        Returns:
            A dict with keys:
            - ``rewired_count`` (int): number of relationships re-pointed.
            - ``deleted_count`` (int): number of duplicate relationships
              removed.
            - ``merged_count`` (int): number of entities soft-deleted.

        Raises:
            NotImplementedError: If the backend cannot provide atomicity.
            NotFoundError: If ``canonical_id`` or any ``merged_id`` does not
                exist.
        """
        ...

    @abstractmethod
    async def create_relationship_bulk(
        self,
        org_id: UUID,
        project_id: UUID,
        relationships: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Batch-create multiple relationships in a single transaction.

        Each dict in ``relationships`` must have ``source_id``, ``target_id``,
        ``relationship_type``.  Optional keys: ``confidence``, ``properties``,
        ``valid_from``, ``valid_to``.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            relationships: List of relationship descriptor dicts.

        Returns:
            List of created relationship dicts (one per input, in the same
            order).  Each includes at minimum ``id``, ``source_id``,
            ``target_id``, ``relationship_type``, and ``created_at``.

        Raises:
            ValueError: If any input dict is missing required keys.
        """
        ...

    # ── Group C: Observations ───────────────────────────────────────────────────

    @abstractmethod
    async def upsert_observation(
        self,
        org_id: UUID,
        project_id: UUID,
        subject_entity_id: UUID,
        observation_type: str,
        content: str,
        confidence: float,
        *,
        related_entity_id: UUID | None = None,
        supporting_fact_ids: list[UUID] | None = None,
        supporting_relationship_ids: list[UUID] | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        observation_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a graph-topology observation.

        Observations are second-pass inferences (co-occurrence, temporal
        patterns) computed by the observation service after initial graph
        construction.

        Upsert uses a functional unique index on
        ``(subject_entity_id, observation_type, COALESCE(related_entity_id, '00000000-0000-0000-0000-000000000000'))``.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            subject_entity_id: The entity this observation is about.
            observation_type: Semantic type label (e.g. ``"co_occurrence"``,
                ``"temporal_gap"``).
            content: Human-readable description of the observation.
            confidence: Confidence score 0.0–1.0.
            related_entity_id: Optional secondary entity involved in the
                observation (e.g. the co-occurring entity).
            supporting_fact_ids: Optional list of fact UUIDs that support this
                observation.
            supporting_relationship_ids: Optional list of relationship UUIDs
                that support this observation.
            valid_from: Optional temporal validity start.
            valid_to: Optional temporal validity end.
            observation_metadata: Optional arbitrary key-value metadata.

        Returns:
            The created or updated observation dict with at minimum ``id``,
            ``subject_entity_id``, ``observation_type``, ``content``,
            ``confidence``, and ``created_at`` keys.
        """
        ...

    @abstractmethod
    async def get_observations(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        subject_entity_id: UUID | None = None,
        observation_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List observations with optional filters and cursor pagination.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            subject_entity_id: Optional filter — only observations about this
                entity.
            observation_type: Optional filter — only observations of this type.
            limit: Maximum results per page. Defaults to 50.
            cursor: Opaque cursor for cursor-based pagination.

        Returns:
            A dict with ``items`` (list of observation dicts),
            ``next_cursor`` (str or None), and ``has_more`` (bool) — same
            pattern as :meth:`list_entities`.
        """
        ...

    @abstractmethod
    async def get_entity_appearance_timestamps(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
    ) -> list[datetime]:
        """Get all timestamps when an entity appeared in episodes.

        Used by temporal gap analysis in the observation service.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            entity_id: UUID of the entity to query.

        Returns:
            Sorted list of episode timestamps (oldest first) when the entity
            appeared.  Empty list if the entity has no linked episodes.
        """
        ...

    @abstractmethod
    async def get_relationship_ids_between(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_a_id: UUID,
        entity_b_id: UUID,
    ) -> list[UUID]:
        """Get IDs of direct relationships between two entities.

        Used by the observation service to provide supporting evidence for
        co-occurrence observations.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            entity_a_id: UUID of the first entity.
            entity_b_id: UUID of the second entity.

        Returns:
            List of relationship UUIDs connecting the two entities (both
            directions).  Empty list if no direct relationship exists.
        """
        ...

    # ── Group C2: Aggregate Queries (for observation service) ────────────────────

    @abstractmethod
    async def get_total_entity_linked_episode_count(
        self,
        org_id: UUID,
        project_id: UUID,
    ) -> int:
        """Get total distinct episodes that have at least one linked entity.

        Used by the observation service to compute co-occurrence confidence.
        Replaces direct SQL on ``graph_episode_entities``.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.

        Returns:
            Total number of distinct episodes in the project that have
            at least one entity linked to them.

        Raises:
            GraphBackendUnavailableError: If the backend is unreachable.
        """
        ...

    @abstractmethod
    async def resolve_entity_names(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_ids: list[UUID],
    ) -> dict[str, dict]:
        """Resolve entity IDs to their names and types.

        Used by the observation service's behavioural pattern detection to
        attach entity metadata to fact-predicate aggregates.  Replaces
        direct SQL join between ``facts`` and ``graph_entities``.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            entity_ids: List of entity UUIDs to resolve.

        Returns:
            Dict keyed by entity ID string, where each value is:
            ``{"name": str, "entity_type": str}``.
            Entity IDs not found in the graph are omitted from the result.

        Raises:
            GraphBackendUnavailableError: If the backend is unreachable.
        """
        ...

    # ── Group D: Soft-Delete / Expiry ──────────────────────────────────────────

    @abstractmethod
    async def expire_relationship(
        self,
        org_id: UUID,
        project_id: UUID,
        relationship_id: UUID,
    ) -> bool:
        """Soft-delete a relationship by setting ``invalid_at``.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            relationship_id: UUID of the relationship to expire.

        Returns:
            ``True`` if the relationship was expired, ``False`` if it did
            not exist or was already expired.
        """
        ...
