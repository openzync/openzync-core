"""Entity repository — interface to Graphiti for entity CRUD operations.

Handles entity node creation, relationship (edge) creation, and entity
lookup in the Graphiti temporal knowledge graph.  Gracefully degrades
when Graphiti is not installed or not initialised — all public methods
return ``None`` when the graph backend is unavailable.

This repository is used by the entity extraction worker and may also be
used by future tasks (entity merge, community summarisation, etc.).

Usage::

    repo = EntityRepository()
    node = await repo.upsert_entity(
        org_id=UUID("..."),
        name="Acme Corp",
        entity_type="Organization",
        summary="A software company",
    )
    edge = await repo.upsert_relationship(
        subject="Alice",
        predicate="works_at",
        obj="Acme Corp",
        org_id=UUID("..."),
    )

TechLead note: This repository talks directly to Graphiti's internal
``_add_entity`` / ``_add_relation`` APIs.  Once the ``GraphBackend``
abstraction (``packages/graphiti-client/interface.py``) is fully adopted,
this repository should delegate to ``FalkorDBBackend`` instead.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

logger = logging.getLogger(__name__)


class EntityRepository:
    """Manages entity nodes and relationships in the knowledge graph.

    Every public method gracefully returns ``None`` when Graphiti is not
    available, allowing callers to operate without conditional guards::

        node = await repo.upsert_entity(...)
        if node is not None:
            # Graphiti was available and the entity was created
            process_node(node)
        else:
            # Graphiti not available — degrade silently
    """

    def __init__(self) -> None:
        self._graphiti: object | None = None
        self._available: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._init_graphiti()

    # ── Initialisation ─────────────────────────────────────────────────────────

    def _init_graphiti(self) -> None:
        """Try to acquire the Graphiti client singleton.

        Sets ``_available`` to ``True`` only if Graphiti is installed
        **and** initialised.  Degrades gracefully in all error cases.
        """
        try:
            from core.graphiti import HAS_GRAPHITI, get_graphiti

            if not HAS_GRAPHITI:
                logger.warning(
                    "entity_repository.graphiti_unavailable",
                    extra={"reason": "graphiti-core is not installed"},
                )
                self._available = False
                return

            client = get_graphiti()
            if client.is_ready:
                # Access the underlying Graphiti SDK instance for direct
                # CRUD operations.
                self._graphiti = client.client
                self._available = True
                logger.info("entity_repository.initialised")
            else:
                logger.warning(
                    "entity_repository.graphiti_not_ready",
                )
                self._available = False
        except RuntimeError:
            # Graphiti was never initialised (e.g. worker process without
            # FastAPI lifespan).  This is expected in the ARQ worker context
            # unless the worker explicitly initialises Graphiti.
            logger.info(
                "entity_repository.graphiti_not_initialised",
                extra={
                    "hint": (
                        "Call init_graphiti() in the worker startup if "
                        "graph-backed entity storage is required."
                    ),
                },
            )
            self._available = False
        except Exception as exc:
            logger.warning(
                "entity_repository.init_failed",
                extra={"error": str(exc)},
            )
            self._available = False

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _run_sync(self, fn: object, *args: object, **kwargs: object) -> object:
        """Offload a synchronous Graphiti call to the thread-pool executor.

        Graphiti's SDK is synchronous — this helper ensures it never blocks
        the asyncio event loop.

        Args:
            fn: The callable to invoke (typically a bound method of the
                Graphiti instance).
            *args: Positional arguments forwarded to *fn*.
            **kwargs: Keyword arguments forwarded to *fn*.

        Returns:
            The return value of *fn*.
        """
        loop = self._loop or asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: fn(*args, **kwargs),  # type: ignore[operator]
        )

    @staticmethod
    def _entity_to_dict(node: object) -> dict:
        """Convert a Graphiti ``EntityNode`` to a plain dictionary.

        Args:
            node: A Graphiti ``EntityNode`` instance (duck-typed to avoid
                a hard import dependency at the module level).

        Returns:
            A serialisable dict with ``id``, ``name``, ``type``,
            ``summary``, and ``created_at`` keys.
        """
        return {
            "id": str(getattr(node, "uuid", "")),
            "name": getattr(node, "name", ""),
            "type": getattr(node, "entity_type", ""),
            "summary": getattr(node, "summary", "") or "",
            "created_at": (
                getattr(node, "created_at", None).isoformat()
                if getattr(node, "created_at", None) is not None
                else None
            ),
        }

    @staticmethod
    def _entity_name_match(node: object, name: str) -> bool:
        """Check whether an entity node's name matches (case-insensitive).

        Args:
            node: A Graphiti ``EntityNode`` or duck-typed equivalent.
            name: The name to compare against.

        Returns:
            ``True`` if the node's name matches *name* (case-insensitive).
        """
        node_name: str = (
            getattr(node, "name", "") or ""
        )
        return node_name.lower().strip() == name.lower().strip()

    # ── Entity CRUD ───────────────────────────────────────────────────────────

    async def upsert_entity(
        self,
        org_id: UUID,
        name: str,
        entity_type: str,
        summary: str | None = None,
    ) -> dict | None:
        """Create or update an entity node in the knowledge graph.

        Uses a name-based lookup (via Graphiti's ``search_entity``) to
        find an existing entity with the same name.  If found, the existing
        node is returned unchanged (no update is performed).  If not found,
        a new entity node is created.

        Args:
            org_id: Organisational scope — the entity belongs to this org.
            name: Canonical display name for the entity (e.g. ``"Acme Corp"``).
            entity_type: Type label — one of ``Person``, ``Organization``,
                ``Product``, ``Location``, ``Date``, ``Custom``.
            summary: Optional text summary or description.

        Returns:
            A dict with ``id``, ``name``, ``type``, ``summary``, and
            ``created_at`` keys, or ``None`` if Graphiti is unavailable.

        Raises:
            Exception: Propagates Graphiti errors when the backend is
                available but the operation fails.
        """
        if not self._available or self._graphiti is None:
            return None

        # ⚠️ Graphiti's search_entity is a fuzzy text search, not an exact
        # match.  We search for the entity name and filter the results
        # case-insensitively to find an exact match.
        existing = await self._find_entity_by_name(org_id, name)
        if existing is not None:
            logger.debug(
                "entity_repository.entity_exists",
                extra={
                    "name": name,
                    "entity_id": existing.get("id"),
                },
            )
            return existing

        # No existing entity found — create a new one.
        # TechLead note: The group_id parameter is the org_id prefixed with
        # "org_" to match Graphiti's organisational isolation convention.
        # This is the same pattern used by sync_to_graph.py.
        try:
            from graphiti_core.nodes import EntityNode

            node: EntityNode = await self._run_sync(
                self._graphiti._add_entity,  # type: ignore[union-attr]  # noqa: SLF001
                org_id=str(org_id),
                name=name,
                entity_type=entity_type,
                summary=summary or "",
            )
            result = self._entity_to_dict(node)
            logger.info(
                "entity_repository.entity_created",
                extra={
                    "org_id": str(org_id),
                    "name": name,
                    "entity_type": entity_type,
                    "entity_id": result["id"],
                },
            )
            return result
        except Exception as exc:
            logger.error(
                "entity_repository.create_failed",
                extra={
                    "org_id": str(org_id),
                    "name": name,
                    "entity_type": entity_type,
                    "error": str(exc),
                },
            )
            raise

    async def _find_entity_by_name(
        self,
        org_id: UUID,
        name: str,
    ) -> dict | None:
        """Look up an entity by exact (case-insensitive) name match.

        Uses Graphiti's ``search_entity`` which performs a fuzzy text
        search.  Results are filtered to find an exact case-insensitive
        match on the entity name.

        Args:
            org_id: Organisational scope.
            name: The entity name to search for.

        Returns:
            The matching entity dict, or ``None`` if no exact match found.
        """
        if not self._available or self._graphiti is None:
            return None

        try:
            results = await self._run_sync(
                self._graphiti.search_entity,  # type: ignore[union-attr]  # noqa: SLF001
                str(org_id),
                name,
                10,
            )
        except AttributeError:
            # Fallback: some Graphiti versions use a different method name.
            try:
                results = await self._run_sync(
                    self._graphiti._search,  # type: ignore[union-attr]  # noqa: SLF001
                    str(org_id),
                    name,
                    10,
                )
            except Exception as fallback_exc:
                logger.warning(
                    "entity_repository.search_fallback_failed",
                    extra={
                        "org_id": str(org_id),
                        "name": name,
                        "error": str(fallback_exc),
                    },
                )
                return None
        except Exception as exc:
            logger.warning(
                "entity_repository.search_failed",
                extra={
                    "org_id": str(org_id),
                    "name": name,
                    "error": str(exc),
                },
            )
            return None

        # Filter results for an exact case-insensitive name match.
        for item in results:
            if self._entity_name_match(item, name):
                return self._entity_to_dict(item)

        return None

    # ── Relationship CRUD ──────────────────────────────────────────────────────

    async def upsert_relationship(
        self,
        subject: str,
        predicate: str,
        obj: str,
        org_id: UUID,
    ) -> dict | None:
        """Create a relationship between two entities in the knowledge graph.

        Looks up both the subject and object entities by name, then creates
        a directed edge from subject to object with the given predicate label.

        Args:
            subject: Name of the source entity (must exist in the graph
                within the given org).
            predicate: Relationship verb in present tense, lowercase
                (e.g. ``"works_at"``, ``"uses"``, ``"located_in"``).
            obj: Name of the target entity (must exist in the graph within
                the given org).
            org_id: Organisational scope for both entities.

        Returns:
            A dict with ``id``, ``source_id``, ``target_id``, ``type``,
            ``properties``, and ``created_at`` keys, or ``None`` if Graphiti
            is unavailable or either entity was not found.
        """
        if not self._available or self._graphiti is None:
            return None

        # Look up both endpoints.
        subject_node = await self._find_entity_by_name(org_id, subject)
        if subject_node is None:
            logger.warning(
                "entity_repository.relationship_subject_not_found",
                extra={
                    "org_id": str(org_id),
                    "subject": subject,
                },
            )
            return None

        object_node = await self._find_entity_by_name(org_id, obj)
        if object_node is None:
            logger.warning(
                "entity_repository.relationship_object_not_found",
                extra={
                    "org_id": str(org_id),
                    "object": obj,
                },
            )
            return None

        # Create the relationship edge.
        try:
            from graphiti_core.edges import GraphRelationship

            edge: GraphRelationship = await self._run_sync(
                self._graphiti._add_relation,  # type: ignore[union-attr]  # noqa: SLF001
                source_node_uuid=subject_node["id"],
                target_node_uuid=object_node["id"],
                relationship=predicate,
                properties={
                    "predicate": predicate,
                    "source_name": subject,
                    "target_name": obj,
                },
            )
            result = self._relationship_to_dict(edge)
            logger.info(
                "entity_repository.relationship_created",
                extra={
                    "org_id": str(org_id),
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "edge_id": result["id"],
                },
            )
            return result
        except Exception as exc:
            logger.error(
                "entity_repository.relationship_create_failed",
                extra={
                    "org_id": str(org_id),
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "error": str(exc),
                },
            )
            raise

    @staticmethod
    def _relationship_to_dict(edge: object) -> dict:
        """Convert a Graphiti ``GraphRelationship`` to a plain dictionary.

        Args:
            edge: A Graphiti ``GraphRelationship`` instance (duck-typed).

        Returns:
            A serialisable dict with ``id``, ``source_id``, ``target_id``,
            ``type``, ``properties``, and ``created_at`` keys.
        """
        return {
            "id": str(getattr(edge, "uuid", "")),
            "source_id": str(getattr(edge, "source_node_uuid", "")),
            "target_id": str(getattr(edge, "target_node_uuid", "")),
            "type": getattr(edge, "relationship_type", ""),
            "properties": getattr(edge, "properties", {}) or {},
            "created_at": (
                getattr(edge, "created_at", None).isoformat()
                if getattr(edge, "created_at", None) is not None
                else None
            ),
        }

    # ── Entity Lookup ─────────────────────────────────────────────────────────

    async def get_entity_by_name(
        self,
        org_id: UUID,
        name: str,
    ) -> dict | None:
        """Retrieve an entity by exact (case-insensitive) name.

        Convenience wrapper around ``_find_entity_by_name`` for external
        callers.

        Args:
            org_id: Organisational scope.
            name: The entity name to search for.

        Returns:
            The matching entity dict, or ``None`` if not found or Graphiti
            is unavailable.
        """
        return await self._find_entity_by_name(org_id, name)

    async def get_entity_by_id(
        self,
        org_id: UUID,
        entity_id: UUID,
    ) -> dict | None:
        """Retrieve an entity by its UUID.

        Args:
            org_id: Organisational scope.
            entity_id: The UUID of the entity to fetch.

        Returns:
            The entity dict, or ``None`` if not found or Graphiti is
            unavailable.
        """
        if not self._available or self._graphiti is None:
            return None

        try:
            node = await self._run_sync(
                self._graphiti._get_entity,  # type: ignore[union-attr]  # noqa: SLF001
                str(org_id),
                str(entity_id),
            )
            if node is None:
                return None
            return self._entity_to_dict(node)
        except Exception as exc:
            logger.error(
                "entity_repository.get_by_id_failed",
                extra={
                    "org_id": str(org_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            return None

    # ── Status ─────────────────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        """``True`` when Graphiti is initialised and ready for operations."""
        return self._available
