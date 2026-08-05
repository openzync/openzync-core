"""Worker-level graph backend resolution — resolves per-org backend for enrichment tasks.

No silent Postgres fallback: a backend name that is configured but cannot be
resolved raises ``GraphBackendUnavailableError`` so the misconfiguration is
visible and alertable.  ``None`` is only returned when graph is explicitly
disabled (no org config, or ``graph_backend`` is ``"none"``/empty).

Usage:

    from workers.backend import resolve_graph_backend

    async def my_worker(ctx, org_id, ...):
        async with db_session_factory() as db:
            backend = await resolve_graph_backend(ctx, org_id, db)
            if backend is None:
                # Graph explicitly disabled for this org — skip graph work
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

logger = logging.getLogger(__name__)


async def resolve_graph_backend(
    ctx: dict[str, Any],
    org_id: UUID,
    db: AsyncSession,
) -> GraphBackend | None:
    """Resolve the per-organization graph backend inside an ARQ worker.

    Uses the ``GraphBackendDispatcher`` from the worker context to instantiate
    the correct backend class based on the org's per-org config.

    Resolution order:
    1. Read ``org_config.graph_backend`` (cache-first via ``core.org_config``,
       DB-authoritative).
    2. No ``graph_backend_dispatcher`` in ctx → worker misconfiguration,
       raises ``GraphBackendUnavailableError``.
    3. No org config → graph disabled, returns ``None``.
    4. Backend name is ``"none"``/empty → graph disabled, returns ``None``.
    5. If SurrealDB is configured, acquire a connection from the shared pool.
    6. Delegate to ``dispatcher.resolve_and_create()`` — any resolution
       failure or a ``None`` result for a configured backend raises
       ``GraphBackendUnavailableError``.  No silent Postgres fallback.

    Args:
        ctx: ARQ worker context dict — must contain
            ``graph_backend_dispatcher`` and may contain
            ``surreal_connection_pool`` and ``falkordb_client``.
        org_id: The organization UUID to resolve the backend for.
        db: An async SQLAlchemy session (for org config queries).

    Returns:
        An initialized ``GraphBackend`` instance, or ``None`` when graph is
        explicitly disabled for the org (no org config, or backend name is
        ``"none"``/empty).

    Raises:
        GraphBackendUnavailableError: If ``graph_backend_dispatcher`` is
            missing from ctx (worker misconfiguration), the dispatcher fails
            to resolve a configured backend, or a configured backend name
            resolves to ``None``.
    """
    dispatcher = ctx.get("graph_backend_dispatcher")
    if dispatcher is None:
        logger.error(
            "worker.no_graph_dispatcher",
            extra={"org_id": str(org_id)},
        )
        raise GraphBackendUnavailableError(
            f"Worker context missing 'graph_backend_dispatcher' for org {org_id}"
        )

    # Fetch per-org config (cache-first via Redis, DB-authoritative)
    org_config = await _resolve_org_config(ctx, org_id, db)

    if org_config is None:
        logger.info(
            "worker.graph_disabled.no_org_config",
            extra={"org_id": str(org_id)},
        )
        return None

    backend_name = org_config.graph_backend
    if not backend_name or backend_name == "none":
        logger.info(
            "worker.graph_disabled.config",
            extra={"org_id": str(org_id), "backend": backend_name},
        )
        return None

    # Get SurrealDB connection — only when the org explicitly configures
    # SurrealDB.  For postgres or none backends the pool is never touched,
    # avoiding unnecessary network round-trips and preventing failures when
    # SurrealDB is down.  If SurrealDB is configured but unreachable the
    # error is raised loudly — no silent fallback to Postgres.
    surreal = None
    if org_config.graph_backend == "surrealdb":
        surreal_pool = ctx.get("surreal_connection_pool")
        if surreal_pool is not None:
            try:
                surreal = await surreal_pool.get_or_create(org_id, org_config)
                logger.debug(
                    "worker.surreal_connection_acquired",
                    extra={"org_id": str(org_id)},
                )
            except GraphBackendUnavailableError:
                raise
            except Exception as exc:
                raise GraphBackendUnavailableError(
                    f"SurrealDB connection failed for org {org_id} "
                    f"with graph_backend='surrealdb': {exc}"
                ) from exc

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
        raise GraphBackendUnavailableError(
            f"Failed to resolve graph backend '{backend_name}' for org {org_id}"
        ) from exc

    if backend is not None:
        logger.info(
            "worker.graph_backend_resolved",
            extra={"org_id": str(org_id), "backend": backend_name},
        )
        return backend

    # A backend name WAS configured but resolve_and_create returned None —
    # treat as a resolution failure, not a silent downgrade.
    logger.error(
        "worker.backend_resolved_to_none",
        extra={"org_id": str(org_id), "backend": backend_name},
    )
    raise GraphBackendUnavailableError(
        f"Graph backend '{backend_name}' resolved to None for org {org_id}"
    )


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
        from core.config import BootstrapSettings
        from core.openbao import OpenBaoClient
        from core.org_config import get_org_config

        redis = ctx.get("redis")  # may not be present in worker ctx
        bao_client = ctx.get("openbao_client")
        if bao_client is None:
            # Fallback: create a short-lived client using bootstrap settings.
            bootstrap = BootstrapSettings()
            async with OpenBaoClient(
                bootstrap.OPENBAO_ADDR,
                bootstrap.OPENBAO_ROLE_ID,
                bootstrap.OPENBAO_SECRET_ID,
                timeout=10.0,
            ) as bao_client:
                return await get_org_config(
                    org_id, redis=redis, bao_client=bao_client
                )
        return await get_org_config(org_id, redis=redis, bao_client=bao_client)
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
