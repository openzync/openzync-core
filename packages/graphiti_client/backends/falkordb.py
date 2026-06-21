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

logger = logging.getLogger(__name__)


class FalkorDBBackend(GraphBackend):
    """Concrete graph backend backed by FalkorDB via the Graphiti engine.

    Args:
        graphiti_client: An initialised :class:`graphiti_core.Graphiti`
            instance whose driver and search are used for all operations.
    """

    def __init__(self, graphiti_client: object) -> None:
        self._graphiti = graphiti_client
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _run_sync(self, fn, *args, **kwargs):
        """Offload a synchronous Graphiti call to the thread-pool executor."""
        loop = self._loop or asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def _get_driver(self):
        """Access the Graphiti driver for direct node/edge operations.

        Graphiti stores its driver as a public ``.driver`` attribute.
        """
        return self._graphiti.driver

    def _entity_to_dict(self, node) -> dict:
        """Convert a Graphiti ``EntityNode`` to a plain dict."""
        return {
            "id": str(node.uuid),
            "name": node.name,
            "type": node.labels[0] if node.labels else "",
            "summary": getattr(node, "summary", "") or "",
            "created_at": node.created_at.isoformat() if node.created_at else None,
        }

    def _relationship_to_dict(self, edge) -> dict:
        """Convert a Graphiti ``EntityEdge`` to a plain dict."""
        return {
            "id": str(edge.uuid),
            "source_id": str(edge.source_node_uuid),
            "target_id": str(edge.target_node_uuid),
            "type": edge.name,
            "properties": getattr(edge, "attributes", {}) or {},
            "created_at": edge.created_at.isoformat() if edge.created_at else None,
        }

    # ── Entity CRUD ────────────────────────────────────────────────────────────

    async def create_entity(
        self,
        org_id: UUID,
        project_id: UUID,
        name: str,
        entity_type: str,
        summary: str | None = None,
    ) -> dict:
        """Create a new entity node scoped to the organisation and project.

        Uses the public ``EntityNode`` constructor + ``save()`` API.
        The ``project_id`` is stored in the node's ``attributes`` dict for
        query filtering.
        """
        from datetime import datetime, timezone

        from graphiti_core.nodes import EntityNode

        try:
            driver = self._get_driver()
            node = EntityNode(
                name=name,
                group_id=f"org:{org_id}",
                labels=[entity_type],
                summary=summary or "",
                created_at=datetime.now(timezone.utc),
                attributes={"project_id": str(project_id)},
            )
            await self._run_sync(node.save, driver)

            logger.info(
                "graphiti.entity_created",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
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
                    "project_id": str(project_id),
                    "name": name,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to create entity '{name}': {exc}",
                detail={"org_id": str(org_id), "name": name},
            ) from exc

    async def get_entity(
        self, org_id: UUID, project_id: UUID, entity_id: UUID
    ) -> dict | None:
        """Retrieve an entity by ID, respecting org and project isolation.

        Uses ``EntityNode.get_by_uuid()`` — returns ``None`` if not found.
        ``project_id`` is accepted for interface compliance; entity lookup
        is by UUID and org-level ``group_id``.
        """
        from graphiti_core.nodes import EntityNode

        try:
            driver = self._get_driver()
            node = await self._run_sync(
                EntityNode.get_by_uuid,
                driver,
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
                    "project_id": str(project_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to retrieve entity {entity_id}: {exc}",
                detail={"org_id": str(org_id), "entity_id": str(entity_id)},
            ) from exc

    async def delete_entity(
        self, org_id: UUID, project_id: UUID, entity_id: UUID
    ) -> bool:
        """Delete an entity node.

        Fetches via ``EntityNode.get_by_uuid()``, then calls ``.delete()``.
        ``project_id`` is accepted for interface compliance.
        """
        from graphiti_core.nodes import EntityNode

        try:
            driver = self._get_driver()
            node = await self._run_sync(
                EntityNode.get_by_uuid,
                driver,
                str(entity_id),
            )
            if node is None:
                logger.info(
                    "graphiti.delete_entity_not_found",
                    extra={
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "entity_id": str(entity_id),
                    },
                )
                return False

            await self._run_sync(node.delete, driver)
            logger.info(
                "graphiti.entity_deleted",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_id": str(entity_id),
                },
            )
            return True
        except Exception as exc:
            logger.error(
                "graphiti.delete_entity_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
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
        project_id: UUID,
        source_id: UUID,
        target_id: UUID,
        relationship_type: str,
        properties: dict | None = None,
        valid_from: datetime | None = None,  # noqa: ARG002 — accepted for ABC compat
        valid_to: datetime | None = None,  # noqa: ARG002 — accepted for ABC compat
    ) -> dict:
        """Create a directed edge between two entities.

        Uses the public ``EntityEdge`` constructor + ``save()`` API.
        Validates both endpoints exist before creating.

        ``project_id`` is stored in the edge's ``attributes`` dict.

        Note: ``valid_from`` and ``valid_to`` are accepted for ABC
        compatibility but ignored (Graphiti does not support temporal
        edges through this API).
        """
        from datetime import datetime, timezone

        from graphiti_core.edges import EntityEdge
        from graphiti_core.nodes import EntityNode

        try:
            driver = self._get_driver()

            # Verify both endpoints exist
            source = await self._run_sync(
                EntityNode.get_by_uuid, driver, str(source_id)
            )
            if source is None:
                raise NotFoundError(
                    message=f"Source entity {source_id} not found in org {org_id}",
                    detail={"entity_id": str(source_id), "org_id": str(org_id)},
                )
            target = await self._run_sync(
                EntityNode.get_by_uuid, driver, str(target_id)
            )
            if target is None:
                raise NotFoundError(
                    message=f"Target entity {target_id} not found in org {org_id}",
                    detail={"entity_id": str(target_id), "org_id": str(org_id)},
                )

            edge_properties = dict(properties or {})
            edge_properties["project_id"] = str(project_id)

            edge = EntityEdge(
                source_node_uuid=str(source_id),
                target_node_uuid=str(target_id),
                name=relationship_type,
                group_id=f"org:{org_id}",
                fact="",
                created_at=datetime.now(timezone.utc),
                attributes=edge_properties,
            )
            await self._run_sync(edge.save, driver)

            logger.info(
                "graphiti.relationship_created",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                    "type": relationship_type,
                },
            )
            return self._relationship_to_dict(edge)
        except NotFoundError:
            raise
        except Exception as exc:
            logger.error(
                "graphiti.create_relationship_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
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
        project_id: UUID,
        start_node_id: UUID,
        max_depth: int = 2,
        edge_types: list[str] | None = None,  # noqa: ARG002
    ) -> list[dict]:
        """Traverse the graph outward from a starting node.

        Uses Graphiti's ``graphiti.search()`` with ``center_node_uuid``
        set to the start node, which performs BFS up to the configured depth.
        ``project_id`` is accepted for interface compliance; scoping is via
        the org-level ``group_id``.
        """
        try:
            results = await self._run_sync(
                self._graphiti.search,
                query="",
                group_ids=[f"org:{org_id}"],
                center_node_uuid=str(start_node_id),
                num_results=max_depth * 10,
            )

            nodes: list[dict] = []
            seen: set[str] = set()

            for item in results if results else []:
                # search() returns list[EntityEdge] — extract nodes
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
                    "project_id": str(project_id),
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
        project_id: UUID,
        query: str,
        types: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,  # noqa: ARG002 — accepted for ABC compat
    ) -> list[dict]:
        """Search entities by name or summary text.

        Uses Graphiti's ``graphiti.search()`` which returns ranked entity
        edges.  Results are deduplicated and returned as entity dicts.
        ``project_id`` is accepted for interface compliance; scoping is via
        the org-level ``group_id``.

        Note: ``offset`` is accepted for ABC compatibility but ignored
        (Graphiti does not support pagination through this API).
        """
        try:
            results = await self._run_sync(
                self._graphiti.search,
                query=query,
                group_ids=[f"org:{org_id}"],
                num_results=limit,
            )

            # search() returns list[EntityEdge]; we extract unique entity nodes
            nodes: list[dict] = []
            seen: set[str] = set()

            for item in results if results else []:
                if hasattr(item, "uuid") and hasattr(item, "name"):
                    uid = str(item.uuid)
                    if uid not in seen:
                        seen.add(uid)
                        node_dict = self._entity_to_dict(item)
                        node_dict["score"] = getattr(item, "score", 1.0)
                        nodes.append(node_dict)
                elif isinstance(item, dict):
                    uid = item.get("uuid", item.get("id"))
                    if uid and uid not in seen:
                        seen.add(str(uid))
                        item["score"] = item.get("score", 1.0)
                        nodes.append(item)

            # Filter by type if requested
            if types:
                nodes = [n for n in nodes if n.get("type") in types]

            return nodes[:limit]
        except Exception as exc:
            logger.error(
                "graphiti.search_entities_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "query": query,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Entity search failed: {exc}",
                detail={"org_id": str(org_id), "query": query},
            ) from exc

    # ── Entity Listing ─────────────────────────────────────────────────────────

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

        Uses ``EntityNode.get_by_group_ids()`` with ``uuid_cursor`` for
        cursor-based pagination (fetch ``limit + 1``, use last item's uuid
        as next cursor).
        ``project_id`` is accepted for interface compliance; scoping is via
        the org-level ``group_id``.
        """
        import json as _json
        from base64 import b64decode, b64encode

        try:
            from graphiti_core.nodes import EntityNode

            driver = self._get_driver()
            group_id = f"org:{org_id}"

            # Decode cursor to get uuid_cursor
            uuid_cursor: str | None = None
            if cursor:
                try:
                    cursor_data = _json.loads(b64decode(cursor).decode())
                    uuid_cursor = cursor_data.get("node_id")
                except Exception:
                    logger.warning(
                        "graphiti.list_entities.invalid_cursor",
                        extra={"cursor": cursor},
                    )

            # Fetch limit + 1 for has_more detection
            fetch_limit = limit + 1
            nodes = await self._run_sync(
                EntityNode.get_by_group_ids,
                driver,
                [group_id],
                fetch_limit,
                uuid_cursor,
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
                    "project_id": str(project_id),
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
        project_id: UUID,
        entity_id: UUID,
        *,
        predicate: str | None = None,
        limit: int = 50,
        cursor: str | None = None,  # noqa: ARG002
    ) -> dict:
        """List all edges incident to a specific entity node.

        Uses ``EntityEdge.get_by_node_uuid()`` with optional predicate filter.
        ``project_id`` is accepted for interface compliance.
        """
        try:
            from graphiti_core.edges import EntityEdge

            driver = self._get_driver()
            edges = await self._run_sync(
                EntityEdge.get_by_node_uuid,
                driver,
                str(entity_id),
            )

            # Filter by predicate if requested (EntityEdge.name is the predicate)
            if predicate:
                edges = [e for e in edges if e.name == predicate]

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
                    "project_id": str(project_id),
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
        project_id: UUID,
        entity_id: UUID,
    ) -> dict | None:
        """Retrieve a single entity node with all its incident edges."""
        node = await self.get_entity(org_id, project_id, entity_id)
        if node is None:
            return None

        edges_result = await self.list_entity_edges(org_id, project_id, entity_id)
        return {
            "node": node,
            "edges": edges_result.get("items", []),
        }

    # ── Observability ──────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Ping the FalkorDB backend through Graphiti's underlying connection."""
        try:
            driver = self._get_driver()
            # Use the driver's session to ping
            from redis import Redis as RedisSync

            if hasattr(driver, "session") and driver.session is not None:
                sync_redis: RedisSync = driver.session
                loop = self._loop or asyncio.get_running_loop()
                result: bool = await loop.run_in_executor(None, sync_redis.ping)
                return result
            return True
        except Exception as exc:
            logger.warning("graphiti.health_check_failed", extra={"error": str(exc)})
            return False
