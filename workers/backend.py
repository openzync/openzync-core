"""Worker-level graph backend resolution — resolves per-user backend for enrichment tasks.

No silent Postgres fallback: a backend name that is configured but cannot be
resolved raises ``GraphBackendUnavailableError`` so the misconfiguration is
visible and alertable.  ``None`` is only returned when graph is explicitly
disabled (``graph_backend`` is ``"none"``/empty); a config-resolution
failure raises ``GraphBackendUnavailableError`` rather than degrading to
"disabled".

Usage:

    from workers.backend import resolve_graph_backend

    async def my_worker(ctx, user_id, ...):
        async with db_session_factory() as db:
            backend = await resolve_graph_backend(ctx, user_id, db)
            if backend is None:
                # Graph explicitly disabled for this user — skip graph work
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
    user_id: UUID,
    db: AsyncSession,
) -> GraphBackend | None:
    """Resolve the per-user graph backend inside an ARQ worker.

    Uses the ``GraphBackendDispatcher`` from the worker context to instantiate
    the correct backend class based on the user's per-user config.

    Resolution order:
    1. Read ``user_config.graph_backend`` (cache-first via ``core.platform_config``,
       DB-authoritative).
    2. No ``graph_backend_dispatcher`` in ctx → worker misconfiguration,
       raises ``GraphBackendUnavailableError``.
    3. No user config → graph disabled, returns ``None``.
    4. Backend name is ``"none"``/empty → graph disabled, returns ``None``.
    5. If SurrealDB is configured, acquire a connection from the shared pool.
    6. Delegate to ``dispatcher.resolve_and_create()`` — any resolution
       failure or a ``None`` result for a configured backend raises
       ``GraphBackendUnavailableError``.  No silent Postgres fallback.

    Args:
        ctx: ARQ worker context dict — must contain
            ``graph_backend_dispatcher`` and may contain
            ``surreal_connection_pool`` and ``falkordb_client``.
        user_id: The user UUID to resolve the backend for.
        db: An async SQLAlchemy session (for user config queries).

    Returns:
        An initialized ``GraphBackend`` instance, or ``None`` when graph is
        explicitly disabled for the user (backend name is ``"none"``/empty).

    Raises:
        GraphBackendUnavailableError: If ``graph_backend_dispatcher`` is
            missing from ctx (worker misconfiguration), the dispatcher fails
            to resolve a configured backend, or a configured backend name
            resolves to ``None``.
            Also raises when user-config resolution fails — a backend must
            never be picked under unknown defaults.
    """
    dispatcher = ctx.get("graph_backend_dispatcher")
    if dispatcher is None:
        logger.error(
            "worker.no_graph_dispatcher",
            extra={"user_id": str(user_id)},
        )
        raise GraphBackendUnavailableError(
            f"Worker context missing 'graph_backend_dispatcher' for user {user_id}"
        )

    # Fetch per-user config (cache-first via Redis, DB-authoritative)
    user_config = await _resolve_user_config(ctx, user_id, db)

    if user_config is None:
        # Defensive: get_platform_config never returns None and the DB fallback
        # raises on failure, so user_config is only None if resolution broke.
        # Fail loud rather than run enrichment as if graph were disabled.
        logger.error(
            "worker.platform_config_unresolved",
            extra={"user_id": str(user_id)},
        )
        raise GraphBackendUnavailableError(
            f"User config for user {user_id} resolved to None — cannot run "
            "enrichment under unknown defaults."
        )

    backend_name = user_config.graph_backend
    if not backend_name or backend_name == "none":
        logger.info(
            "worker.graph_disabled.config",
            extra={"user_id": str(user_id), "backend": backend_name},
        )
        return None

    # Get SurrealDB connection — only when the user selects SurrealDB.
    # Connection details come solely from OZ_SURREALDB_* env vars.  For
    # falkordb or none backends the pool is never touched, avoiding
    # unnecessary network round-trips and preventing failures when
    # SurrealDB is down.  If SurrealDB is selected but unreachable the
    # error is raised loudly — no silent fallback.
    surreal = None
    if backend_name == "surrealdb":
        surreal_pool = ctx.get("surreal_connection_pool")
        if surreal_pool is not None:
            try:
                surreal = await surreal_pool.get_or_create(user_id)
                logger.debug(
                    "worker.surreal_connection_acquired",
                    extra={"user_id": str(user_id)},
                )
            except GraphBackendUnavailableError:
                raise
            except Exception as exc:
                raise GraphBackendUnavailableError(
                    f"SurrealDB connection failed for user {user_id} "
                    f"with graph_backend='surrealdb': {exc}"
                ) from exc

    # SurrealDB configured but no pool / no connection → fail loud.  Do not
    # construct a doomed SurrealGraphBackend(surreal=None) — every op would
    # fail at runtime inside callers that may swallow the error.
    if backend_name == "surrealdb" and surreal is None:
        raise GraphBackendUnavailableError(
            f"SurrealDB configured for user {user_id} (graph_backend="
            f"'surrealdb') but no connection is available — the "
            "surreal_connection_pool is missing or unreachable. "
            "Refusing to continue without a connection."
        )

    # Get FalkorDB client (may be None)
    falkordb_client = ctx.get("falkordb_client")

    # FalkorDB configured but no client → fail loud.  Same reasoning as
    # SurrealDB: a FalkorGraphBackend(client=None) constructs successfully
    # and only fails per-op, which callers may swallow.
    if backend_name == "falkordb" and falkordb_client is None:
        raise GraphBackendUnavailableError(
            f"FalkorDB configured for user {user_id} (graph_backend="
            f"'falkordb') but no client is available in the worker "
            "context. Refusing to continue without a connection."
        )

    try:
        backend: GraphBackend | None = dispatcher.resolve_and_create(
            platform_config=user_config,
            db=db,
            surreal=surreal,
            falkordb_client=falkordb_client,
        )
    except GraphBackendUnavailableError:
        logger.error(
            "worker.backend_unavailable",
            extra={"user_id": str(user_id), "backend": backend_name},
            exc_info=True,
        )
        raise
    except ValueError as exc:
        # Unknown backend name from a configured user — misconfiguration,
        # not "disabled".  Fail loud instead of silently using Postgres.
        logger.error(
            "worker.backend_unknown",
            extra={"user_id": str(user_id), "backend": backend_name},
        )
        raise GraphBackendUnavailableError(
            f"Unknown graph backend '{backend_name}' for user {user_id}: {exc}"
        ) from exc
    except Exception as exc:
        logger.error(
            "worker.backend_resolution_failed",
            extra={
                "user_id": str(user_id),
                "backend": backend_name,
                "error": str(exc),
            },
        )
        raise GraphBackendUnavailableError(
            f"Failed to resolve graph backend '{backend_name}' for user {user_id}"
        ) from exc

    if backend is None:
        # Unreachable for a configured backend (dispatcher either creates an
        # instance or raises) — fail loud rather than silently degrading.
        logger.error(
            "worker.backend_resolved_to_none",
            extra={"user_id": str(user_id), "backend": backend_name},
        )
        raise GraphBackendUnavailableError(
            f"Graph backend '{backend_name}' for user {user_id} resolved to "
            "None but graph is configured — refusing to fall back to Postgres."
        )

    logger.info(
        "worker.graph_backend_resolved",
        extra={"user_id": str(user_id), "backend": backend_name},
    )
    return backend


async def _resolve_user_config(
    ctx: dict[str, Any],
    user_id: UUID,
    db: AsyncSession,
) -> Any | None:
    """Fetch the per-user config, cache-first via ``core.platform_config``, DB-authoritative.

    Uses ``core.platform_config.get_platform_config`` (the standard resolution path)
    if available, which supports Redis caching.  Falls back to a direct DB
    query via ``UserRepository.get_config_override``.

    Args:
        ctx: ARQ worker context dict (may contain ``"redis"`` for caching).
        user_id: The user UUID.
        db: An async SQLAlchemy session.

    Returns:
        An ``PlatformConfigBase`` instance (never ``None`` — an unset backend
        defaults to ``"falkordb"``); raises on resolution failure.
    """
    # ── Primary path: standard user-config resolution (cache-first) ───────
    try:
        from core.platform_config import get_platform_config

        redis = ctx.get("redis")  # may not be present in worker ctx
        return await get_platform_config(user_id, redis=redis, db=db)
    except ImportError:
        logger.debug("worker.platform_config_module_not_available")
    except Exception:
        logger.warning(
            "worker.platform_config_resolution_failed",
            extra={"user_id": str(user_id)},
            exc_info=True,
        )

    # ── Fallback: direct DB query ─────────────────────────────────────────
    try:
        from repositories.user_repository import UserRepository
        from schemas.platform_config import PlatformConfigBase

        repo = UserRepository(db)
        raw_config = await repo.get_config_override(user_id)

        # Flat users.config_override shape — mirror
        # core.platform_config._with_graph_default (unset graph_backend → falkordb;
        # never an all-None wrong default from a dropped nesting level).
        if not raw_config.get("graph_backend"):
            raw_config = {**raw_config, "graph_backend": "falkordb"}
        return PlatformConfigBase(**raw_config)
    except Exception:
        logger.error(
            "worker.platform_config_fallback_failed",
            extra={"user_id": str(user_id)},
            exc_info=True,
        )
        raise GraphBackendUnavailableError(
            f"Failed to resolve user config for user {user_id}: cache and "
            "the DB fallback both failed — cannot run enrichment under "
            "unknown defaults."
        ) from None
