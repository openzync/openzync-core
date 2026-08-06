"""Service dependency factories for FastAPI route injection.

Provides ``Depends``-compatible factory functions that construct domain
service instances with their required dependencies (DB session, Redis, etc.).

Each factory retrieves an ``AsyncSession`` from the DB dependency, creates
the repository, and returns an initialised service.

Usage in a router::

    from fastapi import APIRouter, Depends
    from dependencies.services import get_session_service
    from services.session_service import SessionService

    router = APIRouter()

    @router.get("/sessions")
    async def list_sessions(
        service: SessionService = Depends(get_session_service),
    ):
        ...
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.db import get_db
from dependencies.org_config import get_org_config

if TYPE_CHECKING:
    from core.graph_backend import GraphBackendDispatcher
    from packages.graph_backend.interface import GraphBackend
    from schemas.organization_config import OrgConfigBase
from core.config import get_settings
from core.email import EmailConfig
from core.exceptions import GraphBackendUnavailableError
from middleware.auth_throttle import AuthThrottle
from packages.graph_backend.interface import GraphBackend
from repositories.auth_repository import AuthRepository
from repositories.episode_blob_repository import EpisodeBlobRepository
from repositories.episode_repository import EpisodeRepository
from repositories.fact_repository import FactRepository
from repositories.organization_repository import OrganizationRepository
from repositories.project_repository import ProjectRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from repositories.webhook_repository import WebhookRepository
from services.auth_service import AuthService
from services.email_service import EmailService
from services.fact_service import FactService
from services.graph_service import GraphService
from services.memory_service import MemoryService
from services.otp_service import OtpService
from services.quick_actions_service import QuickActionsService
from services.session_service import SessionService
from services.user_service import UserService
from services.webhook_service import WebhookService

logger = logging.getLogger(__name__)


# ── Webhook (must be first — other factories depend on it) ────────────────────


async def get_webhook_service(
    db: AsyncSession = Depends(get_db),
) -> WebhookService:
    """Dependency that yields an initialised WebhookService.

    Wires in the webhook repository for endpoint CRUD.
    Event emission uses ARQ job delivery (not Svix).
    """
    return WebhookService(
        repo=WebhookRepository(db),
    )


# ── User ───────────────────────────────────────────────────────────────────────


async def get_user_service(
    db: AsyncSession = Depends(get_db),
    webhook: WebhookService = Depends(get_webhook_service),
) -> UserService:
    """Dependency that yields an initialised UserService.

    The service is constructed once per request using a DB session from
    the application's async engine.
    """
    return UserService(repo=UserRepository(db), webhook_service=webhook)


# ── Session ────────────────────────────────────────────────────────────────────


async def get_session_service(
    db: AsyncSession = Depends(get_db),
    webhook: WebhookService = Depends(get_webhook_service),
) -> SessionService:
    """Dependency that yields an initialised SessionService.

    The service is constructed once per request using a DB session from
    the application's async engine.
    """
    return SessionService(repo=SessionRepository(db), webhook_service=webhook)


# ── Auth ───────────────────────────────────────────────────────────────────────


async def get_auth_service(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> AuthService:
    """Dependency that yields an initialised AuthService.

    Wires in the ``AuthRepository``, ``EmailService``, and ``OtpService``
    so that the auth service can send email verification codes during signup.

    Args:
        request: Incoming HTTP request (for ``app.state.redis``).
        db: Async DB session from dependency injection.

    Returns:
        An initialised ``AuthService`` with email verification support.
    """
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        raise RuntimeError(
            "Redis client not found on app.state. "
            "Ensure init_redis() was called during the application lifespan."
        )

    email_config = EmailConfig.from_settings(get_settings())
    email_service = EmailService(email_config)
    otp_service = OtpService(redis=redis_client, email_service=email_service)
    bao_client = getattr(request.app.state, "openbao_client", None)

    return AuthService(
        repo=AuthRepository(db),
        otp_service=otp_service,
        redis=redis_client,
        email_service=email_service,
        bao_client=bao_client,
    )


# ── Fact ────────────────────────────────────────────────────────────────────────


async def get_fact_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
    webhook: WebhookService = Depends(get_webhook_service),
) -> FactService:
    """Dependency that yields an initialised FactService.

    Reads the Redis client from ``request.app.state.redis`` (initialised
    during the application lifespan).  Also passes a lazy per-org graph
    backend resolver so ``ingest_facts`` can wire graph edge expiry on
    supersession; resolution failures are downgraded to warnings inside
    the service and never fail the ingest.
    """
    redis_client = getattr(request.app.state, "redis", None)
    return FactService(
        db=db,
        redis_client=redis_client,
        fact_repo=FactRepository(db),
        session_repo=SessionRepository(db),
        webhook_service=webhook,
        graph_backend_resolver=_make_graph_backend_resolver(request, db),
    )


def _make_graph_backend_resolver(
    request: Request, db: AsyncSession
) -> Callable[[UUID], Awaitable[GraphBackend | None]]:
    """Build a lazy per-org graph-backend resolver from ``request.app.state``.

    Reuses ``workers.backend.resolve_graph_backend`` — the same
    resolution used by the enrichment workers — so the API path and the
    worker path share one implementation (dispatcher, per-org config via
    OpenBao cache-first, SurrealDB pool only when configured, FalkorDB
    client).  Resolution raises ``GraphBackendUnavailableError`` on
    failure; ``FactService.ingest_facts`` catches it and proceeds without
    graph sync (facts are the source of truth).

    Args:
        request: The HTTP request — ``app.state`` carries the graph
            collaborators registered during the lifespan.
        db: The request-scoped session (org-config queries and the
            Postgres backend bind).

    Returns:
        An async callable ``(org_id) -> GraphBackend | None``.
    """
    from workers.backend import resolve_graph_backend

    ctx: dict = {
        "graph_backend_dispatcher": getattr(
            request.app.state, "graph_backend_dispatcher", None
        ),
        "surreal_connection_pool": getattr(
            request.app.state, "surreal_connection_pool", None
        ),
        "falkordb_client": getattr(request.app.state, "falkordb_client", None),
        "openbao_client": getattr(request.app.state, "openbao_client", None),
        "redis": getattr(request.app.state, "redis", None),
    }

    async def _resolve(org_id: UUID) -> GraphBackend | None:
        return await resolve_graph_backend(ctx, org_id, db)

    return _resolve


# ── Memory ─────────────────────────────────────────────────────────────────────


async def get_memory_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
    webhook: WebhookService = Depends(get_webhook_service),
) -> MemoryService:
    """Dependency that yields an initialised MemoryService.

    Wires up all repositories and Redis with the request-scoped DB session.
    The Redis client is read from ``request.app.state.redis``.
    """
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        raise RuntimeError(
            "Redis client not found on app.state. "
            "Ensure init_redis() was called during the application lifespan."
        )
    return MemoryService(
        db=db,
        redis_client=redis_client,
        episode_repo=EpisodeRepository(db),
        session_repo=SessionRepository(db),
        user_repo=UserRepository(db),
        fact_repo=FactRepository(db),
        org_repo=OrganizationRepository(db),
        webhook_service=webhook,
        blob_repo=EpisodeBlobRepository(db),
    )


# ── Graph ──────────────────────────────────────────────────────────────────────


async def get_graph_service(
    request: Request,
    org_config: OrgConfigBase = Depends(get_org_config),
    db: AsyncSession = Depends(get_db),
    webhook: WebhookService = Depends(get_webhook_service),
) -> GraphService:
    """Dependency that yields an initialised GraphService.

    Uses the ``GraphBackendDispatcher`` (registered in the app lifespan)
    to resolve the per-org backend and create a request-scoped instance.
    Wires in the ``UserRepository`` for user-existence checks and
    ``FactRepository`` for session-scoped entity queries.
    """
    dispatcher: GraphBackendDispatcher = request.app.state.graph_backend_dispatcher

    # Resolve SurrealDB connection only when the org explicitly configures SurrealDB.
    # For postgres or none backends, skip the pool entirely — avoids unnecessary
    # network round-trips and prevents failures when SurrealDB is down.
    surreal = None
    org_id = UUID(request.state.org_id)
    if org_config.graph_backend == "surrealdb":
        pool = request.app.state.surreal_connection_pool
        if pool is not None:
            try:
                settings = get_settings()
                surreal = await pool.get_or_create(
                    org_id, org_config,
                    system_url=settings.SURREALDB_URL,
                )
            except Exception as exc:
                logger.error(
                    "graph_service.surreal_connection_failed",
                    extra={
                        "org_id": str(org_id),
                        "backend": "surrealdb",
                        "error": str(exc),
                    },
                )
                raise GraphBackendUnavailableError(
                    f"SurrealDB connection failed for org {org_id} "
                    f"with graph_backend='surrealdb': {exc}"
                ) from exc

    # SurrealDB configured but no pool / no connection → fail loud rather
    # than constructing a doomed SurrealGraphBackend(surreal=None).
    if org_config.graph_backend == "surrealdb" and surreal is None:
        raise GraphBackendUnavailableError(
            f"SurrealDB configured for org {org_id} (graph_backend="
            f"'surrealdb') but no connection is available — the "
            "surreal_connection_pool is missing or unreachable."
        )

    # Read the FalkorDB client from app state (may be None if not configured).
    # If not configured at system level, try per-org config.
    falkordb_client = getattr(request.app.state, "falkordb_client", None)
    if falkordb_client is None and org_config.falkordb_url:
        try:
            from falkordb.asyncio import FalkorDB
            from redis.asyncio import (
                BlockingConnectionPool as AsyncBlockingConnectionPool,
            )

            pool = AsyncBlockingConnectionPool.from_url(
                org_config.falkordb_url,
                max_connections=5,
                socket_timeout=10,
                socket_keepalive=True,
                decode_responses=True,
            )
            falkordb_client = FalkorDB(connection_pool=pool)
        except Exception as exc:
            logger.error(
                "graph_service.falkordb_per_org_failed",
                extra={"org_id": str(org_id), "error": str(exc)},
            )
            raise GraphBackendUnavailableError(
                f"FalkorDB connection failed for org {org_id}: {exc}"
            ) from exc

    # FalkorDB configured but no client anywhere → fail loud (same rationale).
    if org_config.graph_backend == "falkordb" and falkordb_client is None:
        raise GraphBackendUnavailableError(
            f"FalkorDB configured for org {org_id} (graph_backend="
            f"'falkordb') but no client is available — neither a system-level "
            "FALKORDB_URL nor a per-org falkordb_url."
        )

    try:
        graph_backend = dispatcher.resolve_and_create(
            org_config, db, surreal=surreal, falkordb_client=falkordb_client,
        )
    except GraphBackendUnavailableError:
        raise
    except ValueError as exc:
        # Unknown backend name in a configured org — misconfiguration, not
        # disabled.  Surface as 503 (ServiceUnavailable) rather than 500.
        logger.error(
            "graph_service.unknown_backend",
            extra={
                "org_id": str(org_id),
                "backend": org_config.graph_backend,
            },
        )
        raise GraphBackendUnavailableError(
            f"Unknown graph backend '{org_config.graph_backend}' for org "
            f"{org_id}: {exc}"
        ) from exc

    return GraphService(
        graph_backend=graph_backend,
        user_repo=UserRepository(db),
        fact_repo=FactRepository(db),
        webhook_service=webhook,
    )


# ── Auth Throttle ─────────────────────────────────────────────────────────────


async def get_auth_throttle(
    request: Request,
) -> AuthThrottle:
    """Dependency that yields an initialised AuthThrottle.

    Reads the Redis client from ``request.app.state.redis`` and applies
    the system-level rate-limit settings for IP-based throttling.
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise RuntimeError(
            "Redis client not found on app.state. "
            "Ensure init_redis() was called during the application lifespan."
        )
    settings = get_settings()
    return AuthThrottle(
        redis=redis,
        login_max_per_ip=settings.RATE_LIMIT_IP_MAX,
        login_window_sec=settings.RATE_LIMIT_WINDOW_SEC,
    )


# ── Quick Actions ──────────────────────────────────────────────────────────────


async def get_quick_actions_service(
    db: AsyncSession = Depends(get_db),
) -> QuickActionsService:
    """Dependency that yields an initialised QuickActionsService.

    Wires in the project, user, and organization repositories for
    context-aware action generation.
    """
    return QuickActionsService(
        project_repo=ProjectRepository(db),
        user_repo=UserRepository(db),
        org_repo=OrganizationRepository(db),
    )


# ── Graph Backend (read-only) ────────────────────────────────────────────


async def get_graph_backend_for_project(
    request: Request,
    org_config: OrgConfigBase = Depends(get_org_config),
    db: AsyncSession = Depends(get_db),
) -> GraphBackend:
    """Dependency that resolves and returns a project-scoped graph backend.

    This is a lighter alternative to ``get_graph_service`` for read-only
    queries that only need the backend (not the full ``GraphService``).
    The backend is resolved from the org configuration and returned
    directly — no service wrapping.

    Args:
        request: Incoming HTTP request (for ``app.state`` access).
        org_config: The resolved org configuration including backend type.
        db: Async DB session (required by some backends).

    Returns:
        A ``GraphBackend`` instance configured for the current org.

    Raises:
        GraphBackendUnavailableError: If the selected backend is
            unreachable (e.g. SurrealDB connection fails).
    """
    dispatcher: GraphBackendDispatcher = request.app.state.graph_backend_dispatcher

    surreal = None
    org_id = UUID(request.state.org_id)
    if org_config.graph_backend == "surrealdb":
        pool = request.app.state.surreal_connection_pool
        if pool is not None:
            try:
                from core.config import get_settings

                surreal = await pool.get_or_create(
                    org_id, org_config,
                    system_url=get_settings().SURREALDB_URL,
                )
            except Exception as exc:
                logger.error(
                    "graph_backend.surreal_connection_failed",
                    extra={
                        "org_id": str(org_id),
                        "backend": "surrealdb",
                        "error": str(exc),
                    },
                )
                raise GraphBackendUnavailableError(
                    f"SurrealDB connection failed for org {org_id}: {exc}"
                ) from exc

    falkordb_client = getattr(request.app.state, "falkordb_client", None)
    if falkordb_client is None and org_config.falkordb_url:
        try:
            from falkordb.asyncio import FalkorDB
            from redis.asyncio import (
                BlockingConnectionPool as AsyncBlockingConnectionPool,
            )

            pool = AsyncBlockingConnectionPool.from_url(
                org_config.falkordb_url,
                max_connections=5,
                socket_timeout=10,
                socket_keepalive=True,
                decode_responses=True,
            )
            falkordb_client = FalkorDB(connection_pool=pool)
        except Exception as exc:
            logger.error(
                "graph_backend.falkordb_per_org_failed",
                extra={"org_id": str(org_id), "error": str(exc)},
            )
            raise GraphBackendUnavailableError(
                f"FalkorDB connection failed for org {org_id}: {exc}"
            ) from exc

    # Configured-but-unavailable guards — mirror get_graph_service so the
    # read-only path never constructs a doomed backend or 500s on an
    # unknown backend name.
    if org_config.graph_backend == "surrealdb" and surreal is None:
        raise GraphBackendUnavailableError(
            f"SurrealDB configured for org {org_id} (graph_backend="
            f"'surrealdb') but no connection is available — the "
            "surreal_connection_pool is missing or unreachable."
        )
    if org_config.graph_backend == "falkordb" and falkordb_client is None:
        raise GraphBackendUnavailableError(
            f"FalkorDB configured for org {org_id} (graph_backend="
            f"'falkordb') but no client is available — neither a system-level "
            "FALKORDB_URL nor a per-org falkordb_url."
        )

    try:
        return dispatcher.resolve_and_create(
            org_config, db, surreal=surreal, falkordb_client=falkordb_client,
        )
    except GraphBackendUnavailableError:
        raise
    except ValueError as exc:
        logger.error(
            "graph_backend.unknown_backend",
            extra={
                "org_id": str(org_id),
                "backend": org_config.graph_backend,
            },
        )
        raise GraphBackendUnavailableError(
            f"Unknown graph backend '{org_config.graph_backend}' for org "
            f"{org_id}: {exc}"
        ) from exc



