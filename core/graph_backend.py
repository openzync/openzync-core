"""Graph backend registry + resolver — selects backend based on per-user config.

The :class:`GraphBackendDispatcher` is an app-level singleton that holds a
registry of backend **classes** (not instances).  Callers use
:meth:`resolve_and_create` to obtain a request-scoped backend instance:

Usage::

    from core.graph_backend import init_dispatcher

    # App startup:
    app.state.graph_backend_dispatcher = init_dispatcher()

    # Per-request (FalkorDB):
    dispatcher = request.app.state.graph_backend_dispatcher
    backend = dispatcher.resolve_and_create(
        platform_config, db, falkordb_client=falkordb
    )
    entity = await backend.create_entity(user_id=..., name="Acme", ...)

    # Per-request (SurrealDB):
    backend = dispatcher.resolve_and_create(platform_config, db, surreal=surreal)

    The default backend is ``"falkordb"`` (set in
:class:`~schemas.platform_config.PlatformConfigBase`); ``"none"``
disables the graph entirely. ``"postgres"`` was removed in v1.1.0
and now returns HTTP 410 Gone.

To add a new backend in the future:

1. Create a class implementing :class:`GraphBackend`.
2. Register it: ``dispatcher.register("my_backend", MyBackend)``.
3. Set ``platform_config.graph_backend = "my_backend"`` for the target platform.

No callers change.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from core.exceptions import GraphBackendUnavailableError
from packages.graph_backend.interface import GraphBackend

if TYPE_CHECKING:
    from schemas.platform_config import PlatformConfigBase

logger = logging.getLogger(__name__)
_resolve_logger = structlog.get_logger(__name__)


class GraphBackendDispatcher:
    """Registry of backend classes + per-user resolution.

    This is an app-level singleton.  It does **not** hold backend instances
    because backends need a request-scoped ``AsyncSurreal`` connection
    (SurrealDB) or ``FalkorDB`` client (FalkorDB).  Instead, it holds the
    **classes** and creates a fresh instance on every call to
    :meth:`resolve_and_create`.

    Example::

        dispatcher = GraphBackendDispatcher()
        dispatcher.register("surrealdb", SurrealGraphBackend)
        dispatcher.register("falkordb", FalkorGraphBackend)

        # Per-request (SurrealDB):
        backend = dispatcher.resolve_and_create(platform_config, db, surreal=surreal)

        # Per-request (FalkorDB):
        backend = dispatcher.resolve_and_create(platform_config, db, falkordb_client=falkordb)
    """

    def __init__(self) -> None:
        self._registry: dict[str, type[GraphBackend]] = {}

    # ── Registration ────────────────────────────────────────────────────────────

    def register(self, name: str, backend_cls: type[GraphBackend]) -> None:
        """Register a backend class under a short name (e.g. ``"falkordb"``).

        If the name is already registered, the new class overwrites the
        previous one.  This is intentional — it allows test suites to
        replace backends without reference counting.

        Args:
            name: Short identifier (e.g. ``"surrealdb"``, ``"falkordb"``).
            backend_cls: A class that implements the ``GraphBackend`` ABC.
        """
        self._registry[name] = backend_cls
        logger.info("graph_backend.registered", extra={"backend": name})

    # ── Resolution + Creation ───────────────────────────────────────────────────

    def resolve_and_create(
        self,
        platform_config: PlatformConfigBase | None,
        db: AsyncSession,
        surreal: Any = None,
        falkordb_client: Any = None,
    ) -> GraphBackend | None:
        """Resolve the backend name from ``platform_config`` and create an instance.

        Steps:

        1. If ``platform_config`` is ``None`` or ``platform_config.graph_backend``
           is not set or equals ``"none"`` → returns ``None`` (graph disabled).
        2. Looks up the backend name in the registry.
        3. Creates a new instance with backend-specific kwargs.

        Currently supported backends:

        - **``"surrealdb"``**: Creates a :class:`SurrealGraphBackend`.
          Receives ``surreal`` and ``graph_max_traversal_depth``.
        - **``"falkordb"``**: Creates a :class:`FalkorGraphBackend`.
          Receives ``client`` (the ``FalkorDB`` instance) and
          ``max_traversal_depth``.

        Args:
            platform_config: The resolved platform configuration.  May be ``None``
                (treated as graph disabled).
            db: Unused — kept for signature compatibility.  Backends take
                ``surreal`` / ``falkordb_client``, never ``db``.
            surreal: An optional ``AsyncSurreal`` instance from the per-user
                connection pool.  Passed only to the SurrealDB backend.
                May be ``None``. Raises ``GraphBackendUnavailableError``
                when backend is unavailable.
            falkordb_client: An optional ``FalkorDB`` async client instance
                from the app-level connection pool.  Passed only to the
                FalkorDB backend.  May be ``None``. Raises
                ``GraphBackendUnavailableError`` when backend is unavailable.

        Returns:
            An initialised ``GraphBackend`` instance, or ``None`` if graph
            features are disabled for this user.

        Raises:
            GoneError: If ``graph_backend`` is ``"postgres"`` (removed in v1.1.0).
            ValueError: If the backend name from ``platform_config`` is not
                registered and is not ``"none"``.
        """
        backend_name = self._resolve_backend_name(platform_config)
        if backend_name is None:
            logger.debug("graph_backend.disabled")
            return None

        # Hard reject: postgres removed in v1.1.0 → 410 Gone.  Checked
        # before the registry lookup so stale platform configs get the
        # migration message instead of "Unknown graph backend".
        if backend_name == "postgres":
            logger.error(
                "graph_backend.deprecated_postgres_blocked",
                extra={"backend": backend_name},
            )
            from core.exceptions import GoneError

            raise GoneError(
                "PostgreSQL graph backend deprecated — gone, removed in v1.1.0, "
                "migrate to falkordb (410)"
            )

        cls = self._registry.get(backend_name)
        if cls is None:
            raise ValueError(
                f"Unknown graph backend: '{backend_name}'. "
                f"Available backends: {list(self._registry.keys())}. "
                f"Set graph_backend in the platform configuration "
                f"via PATCH /admin/platform-config."
            )

        # Backend-specific kwargs — each backend receives only the
        # arguments it needs.
        kwargs: dict[str, Any] = {}
        if backend_name == "surrealdb":
            if surreal is not None:
                kwargs["surreal"] = surreal
            if (
                platform_config is not None
                and platform_config.graph_max_traversal_depth is not None
            ):
                kwargs["max_traversal_depth"] = platform_config.graph_max_traversal_depth
        elif backend_name == "falkordb":
            if falkordb_client is not None:
                kwargs["client"] = falkordb_client
            if (
                platform_config is not None
                and platform_config.graph_max_traversal_depth is not None
            ):
                kwargs["max_traversal_depth"] = platform_config.graph_max_traversal_depth

        backend = cls(**kwargs)
        logger.info(
            "graph_backend.instance_created",
            extra={"backend": backend_name},
        )
        return backend

    def create_all_backends(
        self,
        db: AsyncSession,
        platform_config: PlatformConfigBase | None = None,
        surreal: Any = None,
        falkordb_client: Any = None,
    ) -> list[GraphBackend]:
        """Create one instance of every registered backend.

        This is the multi-backend equivalent of ``resolve_and_create``.
        Instead of picking one backend from the platform config, it creates
        **all** registered backends.  Each backend receives backend-
        specific kwargs (``surreal`` for SurrealDB, ``client`` for
        FalkorDB) and any shared kwargs from ``platform_config``.

        Callers (e.g. ``HybridRetriever``) run these backends in parallel
        and merge results. Raises ``GraphBackendUnavailableError`` when no
        backends are registered.

        Args:
            db: Unused (kept for signature compatibility).
            platform_config: Optional per-user config for backend-specific kwargs
                such as ``graph_max_traversal_depth``.
            surreal: An optional ``AsyncSurreal`` instance from the per-user
                connection pool.  Only passed to the SurrealDB backend.
            falkordb_client: An optional ``FalkorDB`` async client instance
                from the app-level connection pool.  Only passed to the
                FalkorDB backend.

        Returns:
            A list of initialised ``GraphBackend`` instances (may be empty).
        """
        instances: list[GraphBackend] = []
        for backend_name, cls in self._registry.items():
            kwargs: dict[str, Any] = {}
            if backend_name == "surrealdb":
                if surreal is None:
                    continue
                kwargs["surreal"] = surreal
                if (
                    platform_config is not None
                    and platform_config.graph_max_traversal_depth is not None
                ):
                    kwargs["max_traversal_depth"] = platform_config.graph_max_traversal_depth
            elif backend_name == "falkordb":
                if falkordb_client is None:
                    continue
                kwargs["client"] = falkordb_client
                if (
                    platform_config is not None
                    and platform_config.graph_max_traversal_depth is not None
                ):
                    kwargs["max_traversal_depth"] = platform_config.graph_max_traversal_depth
            instances.append(cls(**kwargs))

        logger.info(
            "graph_backend.all_created",
            extra={"count": len(instances), "backends": list(self._registry.keys())},
        )
        return instances

    # ── Resolution only (no instance) ────────────────────────────────────────────

    def resolve_backend_name(self, platform_config: PlatformConfigBase | None) -> str | None:
        """Determine which backend name this user should use.

        This is a pure lookup — it does **not** create an instance.
        Useful when the caller only needs to know *which* backend is
        configured without instantiating it.

        Args:
            platform_config: The resolved platform configuration.

        Returns:
            The backend name string (e.g. ``"falkordb"``), or ``None`` if
            graph features are disabled.
        """
        return self._resolve_backend_name(platform_config)

    # ── Internals ───────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_backend_name(platform_config: PlatformConfigBase | None) -> str | None:
        """Extract the backend name from platform config.

        Returns ``None`` when the graph is intentionally disabled
        (config is ``None``, field is empty or ``"none"``).
        """
        if platform_config is None:
            return None
        backend_name = platform_config.graph_backend
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
    The SurrealDB backend is optional (``surrealdb`` extra): when the
    package is not installed the import is skipped and selecting
    ``graph_backend="surrealdb"`` fails loud at resolve time.
    """
    from packages.graph_backend.falkordb import FalkorGraphBackend

    dispatcher = GraphBackendDispatcher()
    dispatcher.register("falkordb", FalkorGraphBackend)
    try:
        from packages.graph_backend.surrealdb import SurrealGraphBackend
    except ImportError:
        logger.warning(
            "graph_backend.surrealdb_extra_missing",
            extra={"hint": "pip install openzync[surreal]"},
        )
    else:
        dispatcher.register("surrealdb", SurrealGraphBackend)
    return dispatcher


async def resolve_graph_backend_fail_soft(
    request: Request,
    platform_config: PlatformConfigBase,
    user_id: UUID,
    db: AsyncSession,
    log_prefix: str,
) -> GraphBackend | None:
    """Resolve the platform-configured graph backend, fail-soft to ``None``.

    Single home for the dispatcher + SurrealDB-pool + resolve sequence
    previously triplicated across the admin metrics/stats services. A down
    or misconfigured graph must not take down the dashboard — every
    degraded path logs and returns ``None`` (callers render zeros).

    Args:
        request: FastAPI request (dispatcher + pools live on app state).
        platform_config: Platform config (graph backend selection).
        user_id: Authenticated user ID (singleton platform) (log context + pool key).
        db: Request-scoped session passed to ``resolve_and_create``.
        log_prefix: Caller namespace for log events
            (``"admin_metrics"`` / ``"admin_stats"``).

    Returns:
        The resolved backend, or ``None`` when the dispatcher is missing,
        SurrealDB fails, or resolution raises.
    """
    dispatcher = getattr(request.app.state, "graph_backend_dispatcher", None)
    if dispatcher is None:
        _resolve_logger.warning(f"{log_prefix}.no_graph_dispatcher")
        return None
    surreal = None
    if platform_config.graph_backend == "surrealdb":
        pool = getattr(request.app.state, "surreal_connection_pool", None)
        if pool is not None:
            try:
                surreal = await pool.get_or_create(user_id)
            except Exception as exc:
                _resolve_logger.warning(
                    f"{log_prefix}.surreal_connection_failed",
                    error=str(exc),
                )
                return None
    try:
        return dispatcher.resolve_and_create(
            platform_config,
            db,
            surreal=surreal,
            falkordb_client=getattr(request.app.state, "falkordb_client", None),
        )
    except (GraphBackendUnavailableError, ValueError) as exc:
        _resolve_logger.warning(
            f"{log_prefix}.graph_backend_unresolved", error=str(exc)
        )
        return None
    except Exception as exc:
        _resolve_logger.warning(f"{log_prefix}.graph_backend_failed", error=str(exc))
        return None
