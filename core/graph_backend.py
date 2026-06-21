"""Graph backend factory — selects backend based on per-org config.

Usage::

    from core.graph_backend import init_graph_backend

    backend = await init_graph_backend(db=async_session, org_config=org_config)
    entity = await backend.create_entity(org_id=..., name="Acme", ...)

There are **no** defaults.  ``org_config`` must contain ``graph_backend``.
If the graph is intentionally disabled,
set ``graph_backend`` to ``"none"`` in the per-org config.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.graph_backend.interface import GraphBackend

if TYPE_CHECKING:
    from schemas.organization_config import OrgConfigBase

logger = logging.getLogger(__name__)


async def init_graph_backend(
    db: AsyncSession | None = None,
    org_config: OrgConfigBase | None = None,
) -> GraphBackend | None:
    """Initialise the configured graph backend.

    **There are no defaults.**  ``org_config`` must be provided and must
    contain ``graph_backend``.  If the graph is intentionally disabled,
    set ``graph_backend`` to ``"none"`` in the per-org config.

    Supported backends:

    - ``"postgres"``: :class:`PostgresGraphBackend` (requires ``db`` and
      ``graph_max_traversal_depth`` in ``org_config``)
    - ``"none"``: returns ``None`` — graph features disabled

    Args:
        db: An async SQLAlchemy session. Required for ``postgres`` backend.
        org_config: **Required** — the resolved per-org configuration.
            Must contain ``graph_backend``.  ``graph_max_traversal_depth``
            is required for ``postgres``.

    Returns:
        An initialised ``GraphBackend`` instance, or ``None`` if graph
        features are disabled.

    Raises:
        ValueError: If ``org_config`` is ``None``, or required fields are
            missing, or the backend name is unknown.
    """
    backend_name = _resolve_backend(org_config)

    if backend_name == "postgres":
        if db is None:
            raise ValueError("db is required for postgres graph backend")
        from packages.graph_backend.postgres import PostgresGraphBackend

        if org_config is None or org_config.graph_max_traversal_depth is None:
            raise ValueError(
                "graph_max_traversal_depth is required in per-org "
                "configuration for the postgres graph backend. "
                "Set it via PATCH /admin/org/config."
            )
        max_depth = org_config.graph_max_traversal_depth
        backend: GraphBackend = PostgresGraphBackend(db, max_traversal_depth=max_depth)
        logger.info("graph_backend.initialized", extra={"backend": "postgres"})
        return backend

    elif backend_name == "none":
        logger.info("graph_backend.disabled")
        return None

    else:
        raise ValueError(f"Unknown graph backend: {backend_name}")


def _resolve_backend(
    org_config: OrgConfigBase | None = None,
) -> str:
    """Resolve the effective backend name.

    ``org_config`` is **required** — there is no fallback default.
    The caller must have ``graph_backend`` set in the per-org config.
    Use ``"none"`` to disable graph features.

    Args:
        org_config: The resolved per-org configuration.  Must contain
            ``graph_backend``.

    Returns:
        The resolved backend name (``"postgres"`` or ``"none"``).

    Raises:
        ValueError: If ``org_config`` is ``None`` or ``graph_backend``
            is not set.
    """
    if org_config is None or not org_config.graph_backend:
        raise ValueError(
            "graph_backend is not configured. "
            "Set graph_backend in the per-org configuration "
            "via PATCH /admin/org/config."
        )
    return org_config.graph_backend
