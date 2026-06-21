"""Graph backend registry + resolver — selects backend based on per-org config.

The :class:`GraphBackendDispatcher` is an app-level singleton that holds a
registry of backend **classes** (not instances).  Callers use
:meth:`resolve_and_create` to obtain a request-scoped backend instance:

Usage::

    from core.graph_backend import init_dispatcher

    # App startup:
    app.state.graph_backend_dispatcher = init_dispatcher()

    # Per-request:
    dispatcher = request.app.state.graph_backend_dispatcher
    backend = dispatcher.resolve_and_create(org_config, db)
    entity = await backend.create_entity(org_id=..., name="Acme", ...)

There are **no** defaults.  ``org_config`` must contain ``graph_backend``.
If the graph is intentionally disabled,
set ``graph_backend`` to ``"none"`` in the per-org config.

To add a new backend in the future:

1. Create a class implementing :class:`GraphBackend`.
2. Register it: ``dispatcher.register("my_backend", MyBackend)``.
3. Set ``org_config.graph_backend = "my_backend"`` for the target org.

No callers change.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from packages.graph_backend.interface import GraphBackend

if TYPE_CHECKING:
    from schemas.organization_config import OrgConfigBase

logger = logging.getLogger(__name__)


class GraphBackendDispatcher:
    """Registry of backend classes + per-org resolution.

    This is an app-level singleton.  It does **not** hold backend instances
    because backends need a request-scoped ``AsyncSession``.  Instead, it
    holds the **classes** and creates a fresh instance on every call to
    :meth:`resolve_and_create`.

    Example::

        dispatcher = GraphBackendDispatcher()
        dispatcher.register("postgres", PostgresGraphBackend)
        # future: dispatcher.register("neo4j", Neo4jGraphBackend)

        # Per-request:
        backend = dispatcher.resolve_and_create(org_config, db)
    """

    def __init__(self) -> None:
        self._registry: dict[str, type[GraphBackend]] = {}

    # ── Registration ────────────────────────────────────────────────────────────

    def register(self, name: str, backend_cls: type[GraphBackend]) -> None:
        """Register a backend class under a short name (e.g. ``"postgres"``).

        If the name is already registered, the new class overwrites the
        previous one.  This is intentional — it allows test suites to
        replace backends without reference counting.

        Args:
            name: Short identifier (e.g. ``"postgres"``, ``"neo4j"``).
            backend_cls: A class that implements the ``GraphBackend`` ABC.
        """
        self._registry[name] = backend_cls
        logger.info("graph_backend.registered", extra={"backend": name})

    # ── Resolution + Creation ───────────────────────────────────────────────────

    def resolve_and_create(
        self,
        org_config: OrgConfigBase | None,
        db: AsyncSession,
    ) -> GraphBackend | None:
        """Resolve the backend name from ``org_config`` and create an instance.

        Steps:

        1. If ``org_config`` is ``None`` or ``org_config.graph_backend``
           is not set or equals ``"none"`` → returns ``None`` (graph disabled).
        2. Looks up the backend name in the registry.
        3. Creates a new instance with the given ``db`` and any
           backend-specific kwargs extracted from ``org_config``.

        Currently supported backends:

        - **``"postgres"``**: Creates a :class:`PostgresGraphBackend`.
          Reads ``graph_max_traversal_depth`` from ``org_config``.

        Args:
            org_config: The resolved per-org configuration.  May be ``None``
                (treated as graph disabled).
            db: A request-scoped ``AsyncSession``.  Required for backends
                that use SQL (all current implementations).

        Returns:
            An initialised ``GraphBackend`` instance, or ``None`` if graph
            features are disabled for this org.

        Raises:
            ValueError: If the backend name from ``org_config`` is not
                registered and is not ``"none"``.
        """
        backend_name = self._resolve_backend_name(org_config)
        if backend_name is None:
            logger.debug("graph_backend.disabled")
            return None

        cls = self._registry.get(backend_name)
        if cls is None:
            raise ValueError(
                f"Unknown graph backend: '{backend_name}'. "
                f"Available backends: {list(self._registry.keys())}. "
                f"Set graph_backend in the per-org configuration "
                f"via PATCH /admin/org/config."
            )

        # Backend-specific kwargs extracted from org_config
        kwargs: dict = {}
        if backend_name == "postgres":
            if org_config is not None and org_config.graph_max_traversal_depth is not None:
                kwargs["max_traversal_depth"] = org_config.graph_max_traversal_depth

        backend = cls(db=db, **kwargs)
        logger.info(
            "graph_backend.instance_created",
            extra={"backend": backend_name},
        )
        return backend

    # ── Resolution only (no instance) ────────────────────────────────────────────

    def resolve_backend_name(self, org_config: OrgConfigBase | None) -> str | None:
        """Determine which backend name this org should use.

        This is a pure lookup — it does **not** create an instance.
        Useful when the caller only needs to know *which* backend is
        configured without instantiating it.

        Args:
            org_config: The resolved per-org configuration.

        Returns:
            The backend name string (e.g. ``"postgres"``), or ``None`` if
            graph features are disabled.
        """
        return self._resolve_backend_name(org_config)

    # ── Internals ───────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_backend_name(org_config: OrgConfigBase | None) -> str | None:
        """Extract the backend name from org config.

        Returns ``None`` when the graph is intentionally disabled
        (config is ``None``, field is empty or ``"none"``).
        """
        if org_config is None:
            return None
        backend_name = org_config.graph_backend
        if not backend_name or backend_name == "none":
            return None
        return backend_name


def init_dispatcher() -> GraphBackendDispatcher:
    """Create and populate the dispatcher with all registered backends.

    Call once during the application lifespan and store the result in
    ``app.state``::

        from core.graph_backend import init_dispatcher

        app.state.graph_backend_dispatcher = init_dispatcher()

    To add a new backend, import its class and register it here.
    """
    from packages.graph_backend.postgres import PostgresGraphBackend

    dispatcher = GraphBackendDispatcher()
    dispatcher.register("postgres", PostgresGraphBackend)
    # Future: dispatcher.register("neo4j", Neo4jGraphBackend)
    # Future: dispatcher.register("in_memory", InMemoryGraphBackend)
    return dispatcher
