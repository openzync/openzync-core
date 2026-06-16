"""Graph backend factory — selects backend based on config.

Usage::

    from core.graph_backend import init_graph_backend

    backend = await init_graph_backend(db=async_session)
    entity = await backend.create_entity(org_id=..., name="Acme", ...)

Configuration resolution (in priority order):
1. ``org_config`` parameter (per-org DB config, if provided)
2. ``settings.GRAPH_BACKEND`` / ``settings.FALKORDB_URL`` env fallback
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.graphiti_client.interface import GraphBackend

if TYPE_CHECKING:
    from schemas.organization_config import OrgConfigBase

logger = logging.getLogger(__name__)


async def init_graph_backend(
    db: AsyncSession | None = None,
    org_config: OrgConfigBase | None = None,
) -> GraphBackend | None:
    """Initialise the configured graph backend.

    The backend is selected by ``org_config.graph_backend`` (if provided)
    or falls back to ``settings.GRAPH_BACKEND``:

    - ``"postgres"`` (recommended): :class:`PostgresGraphBackend`
    - ``"graphiti"`` (legacy): :class:`FalkorDBBackend` (requires FalkorDB)
    - ``"none"``: returns ``None`` — graph features disabled

    Legacy aliases ``"falkordb"`` and ``"neo4j"`` are mapped to ``"graphiti"``.

    Args:
        db: An async SQLAlchemy session. Required for ``postgres`` backend.
            Ignored for ``graphiti`` backend.
        org_config: Optional resolved org config.  When provided, overrides
            the env-var defaults for backend selection, traversal depth,
            and FalkorDB URL.

    Returns:
        An initialised ``GraphBackend`` instance, or ``None`` if graph
        features are disabled.
    """
    from core.config import settings

    backend_name = _resolve_backend(org_config)

    if backend_name == "postgres":
        if db is None:
            raise ValueError("db is required for postgres graph backend")
        from packages.graphiti_client.backends.postgres import PostgresGraphBackend

        max_depth = (
            org_config.graph_max_traversal_depth
            if org_config
            else getattr(settings, "GRAPH_MAX_TRAVERSAL_DEPTH", 2)
        )
        backend: GraphBackend = PostgresGraphBackend(db, max_traversal_depth=max_depth)
        logger.info("graph_backend.initialized", extra={"backend": "postgres"})
        return backend

    elif backend_name == "graphiti":
        from core.graphiti import init_graphiti, get_graphiti

        falkordb_url = (
            org_config.falkordb_url
            if org_config and org_config.falkordb_url
            else settings.FALKORDB_URL
        )
        try:
            if falkordb_url is None:
                raise ValueError("FALKORDB_URL is required for graphiti backend")
            await init_graphiti(str(falkordb_url))
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


def _resolve_backend(
    org_config: OrgConfigBase | None = None,
) -> str:
    """Resolve the effective backend name, handling legacy aliases.

    Priority:
    1. ``org_config.graph_backend`` (if provided and non-empty)
    2. ``settings.GRAPH_BACKEND`` env fallback
    """
    from core.config import settings

    backend: str = (
        org_config.graph_backend
        if org_config and org_config.graph_backend
        else settings.GRAPH_BACKEND
    )
    alias_map = {
        "falkordb": "graphiti",
        "neo4j": "graphiti",
    }
    return alias_map.get(backend, backend)
