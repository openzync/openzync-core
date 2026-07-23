"""Graph backend registry + resolver — selects backend based on per-org config.

The :class:`GraphBackendDispatcher` is an app-level singleton that holds a
registry of backend **classes** (not instances).  Callers use
:meth:`resolve_and_create` to obtain a request-scoped backend instance:

Usage::

    from core.graph_backend import init_dispatcher

    # App startup:
    app.state.graph_backend_dispatcher = init_dispatcher()

    # Per-request (Postgres):
    dispatcher = request.app.state.graph_backend_dispatcher
    backend = dispatcher.resolve_and_create(org_config, db)
    entity = await backend.create_entity(org_id=..., name="Acme", ...)

    # Per-request (SurrealDB):
    backend = dispatcher.resolve_and_create(org_config, db, surreal=surreal)

    The default backend is ``"surrealdb"`` (set in
:class:`~schemas.organization_config.OrgConfigBase`).
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
    because backends need a request-scoped ``AsyncSession`` (Postgres) or
    ``AsyncSurreal`` connection (SurrealDB).  Instead, it holds the
    **classes** and creates a fresh instance on every call to
    :meth:`resolve_and_create`.

    Example::

        dispatcher = GraphBackendDispatcher()
        dispatcher.register("postgres", PostgresGraphBackend)
        dispatcher.register("surrealdb", SurrealGraphBackend)
        dispatcher.register("falkordb", FalkorGraphBackend)

        # Per-request (Postgres):
        backend = dispatcher.resolve_and_create(org_config, db)

        # Per-request (SurrealDB):
        backend = dispatcher.resolve_and_create(org_config, db, surreal=surreal)

        # Per-request (FalkorDB):
        backend = dispatcher.resolve_and_create(org_config, db, falkordb_client=falkordb)
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
        surreal: Any = None,
        falkordb_client: Any = None,
    ) -> GraphBackend | None:
        """Resolve the backend name from ``org_config`` and create an instance.

        Steps:

        1. If ``org_config`` is ``None`` or ``org_config.graph_backend``
           is not set or equals ``"none"`` → returns ``None`` (graph disabled).
        2. Looks up the backend name in the registry.
        3. Creates a new instance with backend-specific kwargs.

        Currently supported backends:

        - **``"postgres"``**: Creates a :class:`PostgresGraphBackend`.
          Receives ``db`` and ``graph_max_traversal_depth``.
        - **``"surrealdb"``**: Creates a :class:`SurrealGraphBackend`.
          Receives ``surreal`` and ``graph_max_traversal_depth``.
        - **``"falkordb"``**: Creates a :class:`FalkorGraphBackend`.
          Receives ``client`` (the ``FalkorDB`` instance) and
          ``max_traversal_depth``.

        Args:
            org_config: The resolved per-org configuration.  May be ``None``
                (treated as graph disabled).
            db: A request-scoped ``AsyncSession``.  Required for PostgreSQL
                backends.
            surreal: An optional ``AsyncSurreal`` instance from the per-org
                connection pool.  Passed only to the SurrealDB backend.
                May be ``None``. Raises ``GraphBackendUnavailableError``
                when backend is unavailable.
            falkordb_client: An optional ``FalkorDB`` async client instance
                from the app-level connection pool.  Passed only to the
                FalkorDB backend.  May be ``None``. Raises
                ``GraphBackendUnavailableError`` when backend is unavailable.

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

        # Backend-specific kwargs — each backend receives only the
        # arguments it needs.  No ``db=db`` is passed unconditionally
        # because SurrealGraphBackend does not accept it.
        kwargs: dict = {}
        if backend_name == "postgres":
            kwargs["db"] = db
            if (
                org_config is not None
                and org_config.graph_max_traversal_depth is not None
            ):
                kwargs["max_traversal_depth"] = org_config.graph_max_traversal_depth
        elif backend_name == "surrealdb":
            if surreal is not None:
                kwargs["surreal"] = surreal
            if (
                org_config is not None
                and org_config.graph_max_traversal_depth is not None
            ):
                kwargs["max_traversal_depth"] = org_config.graph_max_traversal_depth
        elif backend_name == "falkordb":
            if falkordb_client is not None:
                kwargs["client"] = falkordb_client
            if (
                org_config is not None
                and org_config.graph_max_traversal_depth is not None
            ):
                kwargs["max_traversal_depth"] = org_config.graph_max_traversal_depth

        backend = cls(**kwargs)
        logger.info(
            "graph_backend.instance_created",
            extra={"backend": backend_name},
        )
        return backend

    def create_all_backends(
        self,
        db: AsyncSession,
        org_config: OrgConfigBase | None = None,
        surreal: Any = None,
        falkordb_client: Any = None,
    ) -> list[GraphBackend]:
        """Create one instance of every registered backend.

        This is the multi-backend equivalent of ``resolve_and_create``.
        Instead of picking one backend from the org config, it creates
        **all** registered backends.  Each backend receives backend-
        specific kwargs (``db`` for Postgres, ``surreal`` for SurrealDB,
        ``client`` for FalkorDB) and any shared kwargs from ``org_config``.

        Callers (e.g. ``HybridRetriever``) run these backends in parallel
        and merge results. Raises ``GraphBackendUnavailableError`` when no
        backends are registered.

        Args:
            db: A request-scoped ``AsyncSession``.  Only passed to the
                Postgres backend.
            org_config: Optional per-org config for backend-specific kwargs
                such as ``graph_max_traversal_depth``.
            surreal: An optional ``AsyncSurreal`` instance from the per-org
                connection pool.  Only passed to the SurrealDB backend.
            falkordb_client: An optional ``FalkorDB`` async client instance
                from the app-level connection pool.  Only passed to the
                FalkorDB backend.

        Returns:
            A list of initialised ``GraphBackend`` instances (may be empty).
        """
        instances: list[GraphBackend] = []
        for backend_name, cls in self._registry.items():
            kwargs: dict = {}
            if backend_name == "postgres":
                kwargs["db"] = db
                if (
                    org_config is not None
                    and org_config.graph_max_traversal_depth is not None
                ):
                    kwargs["max_traversal_depth"] = org_config.graph_max_traversal_depth
            elif backend_name == "surrealdb":
                if surreal is None:
                    continue
                kwargs["surreal"] = surreal
                if (
                    org_config is not None
                    and org_config.graph_max_traversal_depth is not None
                ):
                    kwargs["max_traversal_depth"] = org_config.graph_max_traversal_depth
            elif backend_name == "falkordb":
                if falkordb_client is None:
                    continue
                kwargs["client"] = falkordb_client
                if (
                    org_config is not None
                    and org_config.graph_max_traversal_depth is not None
                ):
                    kwargs["max_traversal_depth"] = org_config.graph_max_traversal_depth
            instances.append(cls(**kwargs))

        logger.info(
            "graph_backend.all_created",
            extra={"count": len(instances), "backends": list(self._registry.keys())},
        )
        return instances

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
    from packages.graph_backend.falkordb import FalkorGraphBackend
    from packages.graph_backend.postgres import PostgresGraphBackend
    from packages.graph_backend.surrealdb import SurrealGraphBackend

    dispatcher = GraphBackendDispatcher()
    dispatcher.register("surrealdb", SurrealGraphBackend)
    dispatcher.register("postgres", PostgresGraphBackend)
    dispatcher.register("falkordb", FalkorGraphBackend)
    return dispatcher
