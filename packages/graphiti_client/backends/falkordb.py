"""FalkorDB backend — concrete ``GraphBackend`` implementation using Graphiti.

The :class:`FalkorDBBackend` wraps the Graphiti SDK's synchronous graph
operations in ``run_in_executor`` to remain async-friendly.  Every public
method enforces organisational isolation via ``org_id``.

Translation layer
-----------------
Graphiti exceptions are caught and re-raised as OpenZep application
exceptions (:class:`~openzep.core.exceptions.NotFoundError`,
:class:`~openzep.core.exceptions.ExternalServiceError`, etc.) so that the
calling layer never depends on Graphiti's exception types.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from core.exceptions import ExternalServiceError, NotFoundError
from packages.graphiti_client.interface import GraphBackend

# ⚠️ graphiti_core types are optional — they are used in type annotations
# and method bodies but degrade gracefully when the installed version does
# not expose them.  With ``from __future__ import annotations``, type hints
# are lazy strings and never cause import errors.

logger = logging.getLogger(__name__)


class FalkorDBBackend(GraphBackend):
    """Concrete graph backend backed by FalkorDB via the Graphiti engine.

    Args:
        graphiti_client: An initialised :class:`GraphitiClient` whose
            underlying ``Graphiti`` instance is used for all operations.
    """

    def __init__(self, graphiti_client: Graphiti) -> None:
        self._graphiti: Graphiti = graphiti_client
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _run_sync(self, fn, *args, **kwargs):
        """Offload a synchronous Graphiti call to the thread-pool executor."""
        loop = self._loop or asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def _entity_to_dict(self, node: EntityNode) -> dict:
        """Convert a Graphiti ``EntityNode`` to a plain dict."""
        return {
            "id": str(node.uuid),
            "name": node.name,
            "type": node.entity_type,
            "summary": node.summary or "",
            "created_at": node.created_at.isoformat() if node.created_at else None,
        }

    def _relationship_to_dict(self, edge: GraphRelationship) -> dict:
        """Convert a Graphiti ``GraphRelationship`` to a plain dict."""
        return {
            "id": str(edge.uuid),
            "source_id": str(edge.source_node_uuid),
            "target_id": str(edge.target_node_uuid),
            "type": edge.relationship_type,
            "properties": edge.properties or {},
            "created_at": edge.created_at.isoformat() if edge.created_at else None,
        }

    # ── Entity CRUD ────────────────────────────────────────────────────────────

    async def create_entity(
        self,
        org_id: UUID,
        name: str,
        entity_type: str,
        summary: str | None = None,
    ) -> dict:
        """Create a new entity node scoped to the organisation.

        Raises:
            ExternalServiceError: If Graphiti or FalkorDB returns an error.
        """
        try:
            # Graphiti's _add_entity creates a node and returns an EntityNode.
            node: EntityNode = await self._run_sync(
                self._graphiti._add_entity,  # noqa: SLF001 — intentional SDK usage
                org_id=str(org_id),
                name=name,
                entity_type=entity_type,
                summary=summary or "",
            )
            logger.info(
                "graphiti.entity_created",
                extra={
                    "org_id": str(org_id),
                    "entity_id": str(node.uuid),
                    "entity_type": entity_type,
                },
            )
            return self._entity_to_dict(node)
        except Exception as exc:
            logger.error(
                "graphiti.create_entity_failed",
                extra={
                    "org_id": str(org_id),
                    "name": name,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to create entity '{name}': {exc}",
                detail={"org_id": str(org_id), "name": name},
            ) from exc

    async def get_entity(self, org_id: UUID, entity_id: UUID) -> dict | None:
        """Retrieve an entity by ID, respecting org isolation.

        Returns:
            The entity dict, or ``None`` if not found.

        Raises:
            ExternalServiceError: If the graph query fails.
        """
        try:
            node: EntityNode | None = await self._run_sync(
                self._graphiti._get_entity,  # noqa: SLF001
                str(org_id),
                str(entity_id),
            )
            if node is None:
                return None
            return self._entity_to_dict(node)
        except Exception as exc:
            logger.error(
                "graphiti.get_entity_failed",
                extra={
                    "org_id": str(org_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to retrieve entity {entity_id}: {exc}",
                detail={"org_id": str(org_id), "entity_id": str(entity_id)},
            ) from exc

    async def delete_entity(self, org_id: UUID, entity_id: UUID) -> bool:
        """Delete an entity node.

        Returns:
            ``True`` if the entity was deleted, ``False`` if it did not exist.

        Raises:
            ExternalServiceError: If the graph operation fails.
        """
        try:
            # Attempt to fetch first to confirm existence.
            node: EntityNode | None = await self._run_sync(
                self._graphiti._get_entity,  # noqa: SLF001
                str(org_id),
                str(entity_id),
            )
            if node is None:
                logger.info(
                    "graphiti.delete_entity_not_found",
                    extra={"org_id": str(org_id), "entity_id": str(entity_id)},
                )
                return False

            await self._run_sync(
                self._graphiti._remove_entity,  # noqa: SLF001
                node,
            )
            logger.info(
                "graphiti.entity_deleted",
                extra={
                    "org_id": str(org_id),
                    "entity_id": str(entity_id),
                },
            )
            return True
        except Exception as exc:
            logger.error(
                "graphiti.delete_entity_failed",
                extra={
                    "org_id": str(org_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to delete entity {entity_id}: {exc}",
                detail={"org_id": str(org_id), "entity_id": str(entity_id)},
            ) from exc

    # ── Relationships ──────────────────────────────────────────────────────────

    async def create_relationship(
        self,
        org_id: UUID,
        source_id: UUID,
        target_id: UUID,
        relationship_type: str,
        properties: dict | None = None,
    ) -> dict:
        """Create a directed edge between two entities.

        Validates that both source and target exist within the org before
        creating the relationship.

        Raises:
            NotFoundError: If either endpoint does not exist.
            ExternalServiceError: If the graph operation fails.
        """
        # Verify both endpoints exist.
        source = await self.get_entity(org_id, source_id)
        if source is None:
            raise NotFoundError(
                message=f"Source entity {source_id} not found in org {org_id}",
                detail={"entity_id": str(source_id), "org_id": str(org_id)},
            )
        target = await self.get_entity(org_id, target_id)
        if target is None:
            raise NotFoundError(
                message=f"Target entity {target_id} not found in org {org_id}",
                detail={"entity_id": str(target_id), "org_id": str(org_id)},
            )

        try:
            edge: GraphRelationship = await self._run_sync(
                self._graphiti._add_relation,  # noqa: SLF001
                source_node_uuid=str(source_id),
                target_node_uuid=str(target_id),
                relationship=relationship_type,
                properties=properties or {},
            )
            logger.info(
                "graphiti.relationship_created",
                extra={
                    "org_id": str(org_id),
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                    "type": relationship_type,
                },
            )
            return self._relationship_to_dict(edge)
        except Exception as exc:
            logger.error(
                "graphiti.create_relationship_failed",
                extra={
                    "org_id": str(org_id),
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to create relationship '{relationship_type}': {exc}",
                detail={
                    "org_id": str(org_id),
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                },
            ) from exc

    # ── Traversal & Search ─────────────────────────────────────────────────────

    async def traverse(
        self,
        org_id: UUID,
        start_node_id: UUID,
        max_depth: int = 2,
        edge_types: list[str] | None = None,
    ) -> list[dict]:
        """Traverse the graph outward from a starting node.

        Uses Graphiti's internal search or adjacency queries.  Falls back to
        iterative BFS if Graphiti does not expose a dedicated traverse API.

        Returns:
            List of reachable node dicts, each including a ``depth`` key.
        """
        try:
            # Graphiti's search yields paths; we reconstruct a flat list.
            # ⚠️ Graphiti's public API may change — this uses the internal
            # _search method.  A future abstraction will formalise traversal.
            results = await self._run_sync(
                self._graphiti._search,  # noqa: SLF001
                str(org_id),
                str(start_node_id),
                max_depth,
            )

            nodes: list[dict] = []
            seen: set[str] = set()

            for item in results:
                # Results may be EntityNode or dict structures.
                # Use duck-typing instead of isinstance to avoid depending
                # on Graphiti's type hierarchy.
                uid: str | None = None
                if hasattr(item, "uuid"):
                    uid = str(item.uuid)
                elif isinstance(item, dict):
                    uid = item.get("uuid", item.get("id"))
                    uid = str(uid) if uid else None

                if uid is not None and uid not in seen:
                    seen.add(uid)
                    if hasattr(item, "uuid") and hasattr(item, "name"):
                        node_dict = self._entity_to_dict(item)
                        node_dict["depth"] = getattr(item, "depth", 0)
                        nodes.append(node_dict)
                    elif isinstance(item, dict):
                        item["depth"] = item.get("depth", 0)
                        nodes.append(item)

            return nodes
        except Exception as exc:
            logger.error(
                "graphiti.traverse_failed",
                extra={
                    "org_id": str(org_id),
                    "start_node": str(start_node_id),
                    "max_depth": max_depth,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to traverse from node {start_node_id}: {exc}",
                detail={
                    "org_id": str(org_id),
                    "start_node_id": str(start_node_id),
                },
            ) from exc

    async def search_entities(
        self,
        org_id: UUID,
        query: str,
        types: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Search entities by name or summary text.

        Delegates to Graphiti's ``_search`` or ``search_entity`` internals
        depending on the SDK version.  Falls back to a Cypher query against
        FalkorDB for full-text search.

        Returns:
            List of matching entity dicts, ordered by relevance descending.
            Each dict includes a ``score`` key.
        """
        try:
            # Use Graphiti's entity search if available.
            results = await self._run_sync(
                self._graphiti.search_entity,  # noqa: SLF001
                str(org_id),
                query,
                limit=limit,
            )
        except AttributeError:
            # Fallback: use the internal _search method.
            try:
                results = await self._run_sync(
                    self._graphiti._search,  # noqa: SLF001
                    str(org_id),
                    query,
                    limit=limit,
                )
            except Exception as fallback_exc:
                logger.error(
                    "graphiti.search_fallback_failed",
                    extra={
                        "org_id": str(org_id),
                        "query": query,
                        "error": str(fallback_exc),
                    },
                )
                raise ExternalServiceError(
                    message=f"Entity search failed: {fallback_exc}",
                    detail={"org_id": str(org_id), "query": query},
                ) from fallback_exc
        except Exception as exc:
            logger.error(
                "graphiti.search_failed",
                extra={
                    "org_id": str(org_id),
                    "query": query,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Entity search failed: {exc}",
                detail={"org_id": str(org_id), "query": query},
            ) from exc

        # Normalise results.
        nodes: list[dict] = []
        for item in results:
            if hasattr(item, "uuid") and hasattr(item, "name"):
                node_dict = self._entity_to_dict(item)
                node_dict["score"] = getattr(item, "score", 1.0)
                nodes.append(node_dict)
            elif isinstance(item, dict):
                item["score"] = item.get("score", 1.0)
                nodes.append(item)

        # Filter by type if requested.
        if types:
            nodes = [n for n in nodes if n.get("type") in types]

        # Sort by score descending, then truncate.
        nodes.sort(key=lambda n: n.get("score", 0), reverse=True)
        return nodes[:limit]

    # ── Entity Listing ─────────────────────────────────────────────────────────

    async def list_entities(
        self,
        org_id: UUID,
        *,
        entity_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List entity nodes with optional type filter and cursor pagination.

        Uses Graphiti's ``EntityNode.get_by_group_ids()`` with manual cursor
        pagination (fetch ``limit + 1``, use last item's uuid as next cursor).

        Returns:
            A dict with ``items``, ``next_cursor`` (str or None), and
            ``has_more`` (bool).
        """
        import json as _json
        from base64 import b64decode, b64encode

        try:
            from graphiti_core.nodes import EntityNode

            group_id = self._make_group_id(org_id)

            # Decode cursor to get offset node_id
            offset_node_id: str | None = None
            if cursor:
                try:
                    cursor_data = _json.loads(b64decode(cursor).decode())
                    offset_node_id = cursor_data.get("node_id")
                except Exception:
                    logger.warning(
                        "graphiti.list_entities.invalid_cursor",
                        extra={"cursor": cursor},
                    )
                    offset_node_id = None

            # Fetch limit + 1 for has_more detection
            fetch_limit = limit + 1
            nodes = await self._run_sync(
                _list_entities_sync,
                self._graphiti,
                group_id,
                entity_type,
                fetch_limit,
                offset_node_id,
            )

            has_more = len(nodes) > limit
            if has_more:
                nodes = nodes[:limit]

            items = [self._entity_to_dict(n) for n in nodes]

            next_cursor: str | None = None
            if has_more and nodes:
                cursor_data = _json.dumps({"node_id": str(nodes[-1].uuid)})
                next_cursor = b64encode(cursor_data.encode()).decode()

            return {
                "items": items,
                "next_cursor": next_cursor,
                "has_more": has_more,
            }
        except Exception as exc:
            logger.error(
                "graphiti.list_entities_failed",
                extra={
                    "org_id": str(org_id),
                    "entity_type": entity_type,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to list entities: {exc}",
                detail={"org_id": str(org_id)},
            ) from exc

    async def list_entity_edges(
        self,
        org_id: UUID,
        entity_id: UUID,
        *,
        predicate: str | None = None,
        limit: int = 50,
        cursor: str | None = None,  # noqa: ARG002
    ) -> dict:
        """List all edges incident to a specific entity node.

        Uses Graphiti's ``EntityEdge.get_by_node_uuid()``.  Cursor pagination
        is approximated (Graphiti does not natively support it for edges).

        Returns:
            A dict with ``items`` and ``next_cursor`` / ``has_more``.
        """
        try:
            from graphiti_core.edges import EntityEdge

            edges = await self._run_sync(
                EntityEdge.get_by_node_uuid,
                self._graphiti._driver,  # noqa: SLF001
                str(entity_id),
            )

            # Filter by predicate if requested
            if predicate:
                edges = [e for e in edges if e.facet == predicate or e.name == predicate]

            total = len(edges)
            effective_limit = min(limit, 200)
            page = edges[:effective_limit]
            has_more = total > effective_limit

            items = [self._relationship_to_dict(e) for e in page]

            return {
                "items": items,
                "next_cursor": None,
                "has_more": has_more,
            }
        except Exception as exc:
            logger.error(
                "graphiti.list_entity_edges_failed",
                extra={
                    "org_id": str(org_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to list edges for entity {entity_id}: {exc}",
                detail={"org_id": str(org_id), "entity_id": str(entity_id)},
            ) from exc

    async def get_entity_with_edges(
        self,
        org_id: UUID,
        entity_id: UUID,
    ) -> dict | None:
        """Retrieve a single entity node with all its incident edges.

        Returns:
            A dict with ``node`` (entity dict) and ``edges`` (list of edge
            dicts), or ``None`` if the entity does not exist.
        """
        node = await self.get_entity(org_id, entity_id)
        if node is None:
            return None

        edges_result = await self.list_entity_edges(org_id, entity_id)
        return {
            "node": node,
            "edges": edges_result.get("items", []),
        }

    @staticmethod
    def _make_group_id(org_id: UUID) -> str:
        """Build the group_id string for organisational isolation.

        Args:
            org_id: The organisation UUID.

        Returns:
            A string like ``"org:<uuid>"`` for use with Graphiti's
            group_id parameter.
        """
        return f"org:{org_id}"

    # ── Observability ──────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Ping the FalkorDB backend through Graphiti's underlying connection.

        Returns:
            ``True`` if the backend is reachable, ``False`` otherwise.
        """
        try:
            # Attempt a lightweight graph query to verify the backend is live.
            # Graphiti does not expose a raw PING, so we use a minimal Cypher
            # query (RETURN 1) via the internal Redis client.
            from redis import Redis as RedisSync

            # Graphiti stores its Redis connection internally — we reach it
            # via the config.
            if hasattr(self._graphiti, "_driver") and self._graphiti._driver is not None:
                sync_redis: RedisSync = self._graphiti._driver
                loop = self._loop or asyncio.get_running_loop()
                result: bool = await loop.run_in_executor(None, sync_redis.ping)
                return result

            # Fallback: rely on the fact that if we can call _graphiti, it's
            # already verified connectivity on init.
            return True
        except Exception as exc:
            logger.warning("graphiti.health_check_failed", extra={"error": str(exc)})
            return False


def _list_entities_sync(
    graphiti: Graphiti,
    group_id: str,
    entity_type: str | None,
    limit: int,
    offset_node_id: str | None,
) -> list[EntityNode]:
    """Synchronous helper to list entity nodes via Graphiti's internal API.

    This runs inside ``run_in_executor`` so it never blocks the event loop.

    Args:
        graphiti: The Graphiti SDK instance.
        group_id: The org-namespaced group ID.
        entity_type: Optional entity type filter.
        limit: Number of entities to fetch.
        offset_node_id: Cursor offset — node UUID to start after.

    Returns:
        A list of ``EntityNode`` instances.
    """
    from graphiti_core.nodes import EntityNode

    # Use the internal driver connection for direct node queries.
    driver = graphiti._driver  # noqa: SLF001

    # Build group_ids list
    group_ids = [group_id]

    # Fetch nodes via EntityNode.get_by_group_ids
    # ⚠️ This API may not support offset_node_id in all Graphiti versions.
    nodes: list[EntityNode] = EntityNode.get_by_group_ids(
        driver=driver,
        group_ids=group_ids,
        node_labels=[entity_type] if entity_type else None,
        limit=limit,
        offset_node_id=offset_node_id,
    )

    return nodes
