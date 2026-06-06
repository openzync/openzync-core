"""Graph backend factory — selects backend based on config.

Usage::

    from core.graph_backend import init_graph_backend

    backend = await init_graph_backend(db=async_session)
    entity = await backend.create_entity(org_id=..., name="Acme", ...)
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from packages.graphiti_client.interface import GraphBackend

logger = logging.getLogger(__name__)


async def init_graph_backend(
    db: AsyncSession | None = None,
) -> GraphBackend | None:
    """Initialise the configured graph backend.

    The backend is selected by ``settings.GRAPH_BACKEND``:

    - ``"postgres"`` (recommended): :class:`PostgresGraphBackend`
    - ``"graphiti"`` (legacy): :class:`FalkorDBBackend` (requires FalkorDB)
    - ``"none"``: returns ``None`` — graph features disabled

    Legacy aliases ``"falkordb"`` and ``"neo4j"`` are mapped to ``"graphiti"``
    by the config validator.

    Args:
        db: An async SQLAlchemy session. Required for ``postgres`` backend.
            Ignored for ``graphiti`` backend.

    Returns:
        An initialised ``GraphBackend`` instance, or ``None`` if graph
        features are disabled.
    """
    backend_name = _resolve_backend()

    if backend_name == "postgres":
        if db is None:
            raise ValueError("db is required for postgres graph backend")
        from packages.graphiti_client.backends.postgres import PostgresGraphBackend

        max_depth = getattr(settings, "GRAPH_MAX_TRAVERSAL_DEPTH", 2)
        backend: GraphBackend = PostgresGraphBackend(db, max_traversal_depth=max_depth)
        logger.info("graph_backend.initialized", extra={"backend": "postgres"})
        return backend

    elif backend_name == "graphiti":
        from core.graphiti import init_graphiti, get_graphiti

        try:
            if settings.FALKORDB_URL is None:
                raise ValueError("FALKORDB_URL is required for graphiti backend")
            await init_graphiti(str(settings.FALKORDB_URL))
            client = get_graphiti()
            from packages.graphiti_client.backends.falkordb import FalkorDBBackend

            backend = FalkorDBBackend(client.client)
            logger.info("graph_backend.initialized", extra={"backend": "graphiti"})
            return backend
        except Exception as exc:
            logger.warning(
                "graph_backend.graphiti_failed",
                extra={"error": str(exc)},
            )
            return None

    elif backend_name == "none":
        logger.info("graph_backend.disabled")
        return None

    else:
        raise ValueError(f"Unknown graph backend: {backend_name}")


def _resolve_backend() -> str:
    """Resolve the effective backend name from config, handling aliases.

    The config validator in ``Settings`` maps legacy aliases (``"falkordb"``,
    ``"neo4j"``) to ``"graphiti"``. This function performs a final mapping
    for safety.
    """
    backend = settings.GRAPH_BACKEND
    alias_map = {
        "falkordb": "graphiti",
        "neo4j": "graphiti",
    }
    return alias_map.get(backend, backend)
