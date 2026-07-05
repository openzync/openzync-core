"""Worker-level graph backend resolution — resolves per-org backend for enrichment tasks.

Usage:

    from workers.backend import resolve_graph_backend

    async def my_worker(ctx, org_id, ...):
        async with db_session_factory() as db:
            backend = await resolve_graph_backend(ctx, org_id, db)
            if backend is None:
                # Graph disabled for this org — skip or use Postgres fallback
                ...
            await backend.link_entity_to_episode(...)
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import GraphBackendUnavailableError
from packages.graph_backend.interface import GraphBackend
from packages.graph_backend.postgres import PostgresGraphBackend

logger = logging.getLogger(__name__)


async def resolve_graph_backend(
    ctx: dict[str, Any],
    org_id: UUID,
    db: AsyncSession,
    *,
    fallback_to_postgres: bool = True,
) -> GraphBackend | None:
    """Resolve the per-organization graph backend inside an ARQ worker.

    Uses the ``GraphBackendDispatcher`` from the worker context to instantiate
    the correct backend class based on the org's per-org config.

    Resolution order:
    1. Read ``org_config.graph_backend`` (cache-first via ``core.org_config``,
       DB-authoritative).
    2. If the backend name is ``"none"`` or resolution fails:
       returns ``PostgresGraphBackend`` when *fallback_to_postgres* is
       ``True`` (default), otherwise returns ``None``.

    Args:
        ctx: ARQ worker context dict — must contain
            ``graph_backend_dispatcher`` and may contain
            ``surreal_connection_pool`` and ``falkordb_client``.
        org_id: The organization UUID to resolve the backend for.
        db: An async SQLAlchemy session (for Postgres backend and org config
            queries).
        fallback_to_postgres: If ``True`` (default), returns a
            ``PostgresGraphBackend`` when no backend is configured or
            resolution fails. If ``False``, returns ``None``.

    Returns:
        An initialized ``GraphBackend`` instance, ``None`` if graph is
        disabled and no fallback.

    Raises:
        GraphBackendUnavailableError: If resolution fails and fallback is
            disabled.
    """
    dispatcher = ctx.get("graph_backend_dispatcher")
    if dispatcher is None:
        logger.warning(
            "worker.no_graph_dispatcher.using_postgres_fallback",
            extra={"org_id": str(org_id)},
        )
        return PostgresGraphBackend(db) if fallback_to_postgres else None  # type: ignore[abstract]

    # Fetch per-org config (cache-first via Redis, DB-authoritative)
    org_config = await _resolve_org_config(ctx, org_id, db)

    if org_config is None:
        logger.info(
            "worker.graph_disabled.no_org_config",
            extra={"org_id": str(org_id)},
        )
        return PostgresGraphBackend(db) if fallback_to_postgres else None  # type: ignore[abstract]

    backend_name = org_config.graph_backend
    if not backend_name or backend_name == "none":
        logger.info(
            "worker.graph_disabled.config",
            extra={"org_id": str(org_id), "backend": backend_name},
        )
        return PostgresGraphBackend(db) if fallback_to_postgres else None  # type: ignore[abstract]

    # Get SurrealDB connection (may be None — that's fine, only SurrealDB
    # backend uses it).
    surreal = None
    surreal_pool = ctx.get("surreal_connection_pool")
    if surreal_pool is not None:
        try:
            surreal = await surreal_pool.get_or_create(org_id, org_config)
            logger.debug(
                "worker.surreal_connection_acquired",
                extra={"org_id": str(org_id)},
            )
        except GraphBackendUnavailableError:
            logger.warning(
                "worker.surreal_pool_unavailable",
                extra={"org_id": str(org_id)},
            )
        except Exception:
            logger.warning(
                "worker.surreal_pool_failed",
                extra={"org_id": str(org_id)},
                exc_info=True,
            )

    # Get FalkorDB client (may be None)
    falkordb_client = ctx.get("falkordb_client")

    try:
        backend: GraphBackend | None = dispatcher.resolve_and_create(
            org_config=org_config,
            db=db,
            surreal=surreal,
            falkordb_client=falkordb_client,
        )
    except Exception as exc:
        logger.error(
            "worker.backend_resolution_failed",
            extra={
                "org_id": str(org_id),
                "backend": backend_name,
                "error": str(exc),
            },
        )
        if fallback_to_postgres:
            logger.warning(
                "worker.falling_back_to_postgres",
                extra={"org_id": str(org_id)},
            )
            return PostgresGraphBackend(db)  # type: ignore[abstract]
        raise GraphBackendUnavailableError(
            f"Failed to resolve graph backend '{backend_name}' for org {org_id}"
        ) from exc

    if backend is not None:
        logger.info(
            "worker.graph_backend_resolved",
            extra={"org_id": str(org_id), "backend": backend_name},
        )
        return backend

    logger.warning(
        "worker.backend_resolved_to_none",
        extra={"org_id": str(org_id), "backend": backend_name},
    )
    return PostgresGraphBackend(db) if fallback_to_postgres else None  # type: ignore[abstract]


async def _resolve_org_config(
    ctx: dict[str, Any],
    org_id: UUID,
    db: AsyncSession,
) -> Any | None:
    """Fetch the per-org config, cache-first via ``core.org_config``, DB-authoritative.

    Uses ``core.org_config.get_org_config`` (the standard resolution path)
    if available, which supports Redis caching.  Falls back to a direct DB
    query via ``OrganizationRepository.get_config``.

    Args:
        ctx: ARQ worker context dict (may contain ``"redis"`` for caching).
        org_id: The organization UUID.
        db: An async SQLAlchemy session.

    Returns:
        An ``OrgConfigBase`` instance, or ``None`` if the org does not exist.
    """
    # ── Primary path: standard org config resolution (cache-first) ────────
    try:
        from core.org_config import get_org_config

        # NOTE: signature is get_org_config(org_id, db, redis=None)
        redis = ctx.get("redis")  # may not be present in worker ctx
        return await get_org_config(org_id, db, redis=redis)
    except ImportError:
        logger.debug("worker.org_config_module_not_available")
    except Exception:
        logger.warning(
            "worker.org_config_resolution_failed",
            extra={"org_id": str(org_id)},
            exc_info=True,
        )

    # ── Fallback: direct DB query ─────────────────────────────────────────
    try:
        from repositories.organization_repository import OrganizationRepository
        from schemas.organization_config import OrgConfigBase

        repo = OrganizationRepository(db)
        raw_config = await repo.get_config(org_id)

        # Match core.org_config behavior: empty config → all fields None
        if not raw_config:
            return OrgConfigBase(
                **{name: None for name in OrgConfigBase.model_fields}
            )
        return OrgConfigBase(**raw_config)
    except Exception:
        logger.error(
            "worker.org_config_fallback_failed",
            extra={"org_id": str(org_id)},
            exc_info=True,
        )
        return None
