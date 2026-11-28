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

from dependencies.db import get_db, get_db_admin
from dependencies.platform_config import get_platform_config

if TYPE_CHECKING:
    from core.graph_backend import GraphBackendDispatcher
    from packages.graph_backend.interface import GraphBackend
    from schemas.platform_config import PlatformConfigBase
from core.config import get_settings
from core.email import EmailConfig
from core.exceptions import GraphBackendUnavailableError
from middleware.auth_throttle import AuthThrottle
from packages.graph_backend.interface import GraphBackend
from repositories.auth_repository import AuthRepository
from repositories.episode_blob_repository import EpisodeBlobRepository
from repositories.episode_repository import EpisodeRepository
from repositories.fact_repository import FactRepository
from repositories.instance_settings_repository import (
    InstanceSettingsRepository,
)
from repositories.organization_repository import OrganizationRepository
from repositories.project_repository import ProjectRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from repositories.webhook_repository import WebhookRepository
from services.auth_service import AuthService
from services.email_service import EmailService
from services.fact_service import FactService
from services.graph_service import GraphService
from services.invite_service import InviteService
from services.instance_settings_service import InstanceSettingsService
from services.memory_service import MemoryService
from services.otp_service import OtpService
from services.quick_actions_service import QuickActionsService
from services.session_service import SessionService
from services.setup_service import SetupService
from services.user_config_service import UserConfigService
from services.user_service import UserService
from services.user_summary_service import UserSummaryService
from services.webhook_service import WebhookService

logger = logging.getLogger(__name__)


# ── Webhook (must be first — other factories depend on it) ────────────────────


async def get_webhook_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> WebhookService:
    """Dependency that yields an initialised WebhookService.

    Wires in the webhook repository for endpoint CRUD.  Signing secrets
    are encrypted at rest via ``core.crypto`` (Fernet subkeys from
    ``OZ_SECRET_KEY``).  Event emission uses ARQ job delivery (not Svix).
    """
    return WebhookService(
        repo=WebhookRepository(db),
    )


# ── User ───────────────────────────────────────────────────────────────────────


async def get_user_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
    webhook: WebhookService = Depends(get_webhook_service),
) -> UserService:
    """Dependency that yields an initialised UserService.

    Wires the repository, webhook emitter, and app-level Redis client
    (used for role-cache invalidation on role changes / deletions).

    Args:
        request: Incoming HTTP request (for ``app.state.redis``).
        db: Async DB session from dependency injection.
        webhook: Webhook emitter for user lifecycle events.

    Returns:
        An initialised ``UserService``.
    """
    redis = getattr(request.app.state, "redis", None)
    return UserService(
        repo=UserRepository(db), webhook_service=webhook, redis=redis
    )


async def get_user_summary_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserSummaryService:
    """Dependency that yields an initialised UserSummaryService.

    Args:
        request: Incoming HTTP request (for ``app.state.redis``).
        db: Async DB session from dependency injection.

    Returns:
        An initialised ``UserSummaryService``.
    """
    from core.arq import get_arq

    arq = get_arq()
    redis = getattr(request.app.state, "redis", None)
    return UserSummaryService(db=db, arq=arq, redis=redis)


async def get_admin_user_service(
    request: Request,
    db: AsyncSession = Depends(get_db_admin),
    webhook: WebhookService = Depends(get_webhook_service),
) -> UserService:
    """Dependency that yields an admin-scoped UserService.

    Runs on the RLS-bypass session because the target row belongs to the
    member, not the acting admin (the tenant policy would otherwise
    match zero rows).

    Args:
        request: Incoming HTTP request (for ``app.state.redis``).
        db: RLS-bypass async DB session from dependency injection.
        webhook: Webhook emitter for user lifecycle events.

    Returns:
        An initialised ``UserService`` on the bypass session.
    """
    redis = getattr(request.app.state, "redis", None)
    return UserService(
        repo=UserRepository(db), webhook_service=webhook, redis=redis
    )


# ── Session ────────────────────────────────────────────────────────────────────


async def get_session_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
    webhook: WebhookService = Depends(get_webhook_service),
) -> SessionService:
    """Dependency that yields an initialised SessionService.

    The service is constructed once per request using a DB session from
    the application's async engine.  A lazy per-user graph-backend
    resolver is wired so ``get_messages`` can check entity-link
    presence for ``is_enrichment_skipped``; link lookup degrades to
    unknown (flag stays ``False``) when the backend is unavailable.
    """
    return SessionService(
        repo=SessionRepository(db),
        blob_repo=EpisodeBlobRepository(db),
        webhook_service=webhook,
        graph_backend_resolver=_make_graph_backend_resolver(request, db),
    )


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

    return AuthService(
        repo=AuthRepository(db),
        otp_service=otp_service,
        redis=redis_client,
        email_service=email_service,
        settings_repo=InstanceSettingsRepository(db),
    )


# ── Instance settings ────────────────────────────────────────────────────────


def get_instance_settings_repository(
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> InstanceSettingsRepository:
    """Dependency that yields the singleton settings-row repository.

    Shared by ``SetupService`` (registration mode) and ``AuthService``
    (signup/join gates) — one construction site instead of per-call
    ``InstanceSettingsRepository(...)`` in routers and mixins.

    Args:
        db: Async DB session from dependency injection.

    Returns:
        An initialised ``InstanceSettingsRepository``.
    """
    return InstanceSettingsRepository(db)


async def get_instance_settings_service(
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> InstanceSettingsService:
    """Dependency that yields an InstanceSettingsService (tenant session).

    Serves ``GET /admin/system/registration`` — the singleton settings
    row has no RLS, so the tenant session suffices.

    Args:
        db: Async DB session from dependency injection.

    Returns:
        An initialised ``InstanceSettingsService``.
    """
    return InstanceSettingsService(repo=InstanceSettingsRepository(db))


async def get_admin_instance_settings_service(
    db: AsyncSession = Depends(get_db_admin),  # noqa: B008
) -> InstanceSettingsService:
    """Dependency that yields an InstanceSettingsService (bypass session).

    Serves ``PATCH /admin/system/registration`` — the singleton-row
    write runs on the RLS-bypass session.

    Args:
        db: RLS-bypass async DB session from dependency injection.

    Returns:
        An initialised ``InstanceSettingsService``.
    """
    return InstanceSettingsService(repo=InstanceSettingsRepository(db))


# ── Invite ─────────────────────────────────────────────────────────────────────


async def get_setup_service(
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> SetupService:
    """Dependency that yields an initialised SetupService.

    Wires the request-scoped user and organization repositories for the
    one-time ``POST /v1/auth/setup`` bootstrap.  The endpoint is public,
    so the session carries no tenant — the repositories bypass RLS
    internally for the two platform rows only.

    Args:
        db: Async DB session from dependency injection.

    Returns:
        An initialised ``SetupService``.
    """
    return SetupService(
        user_repo=UserRepository(db),
        org_repo=OrganizationRepository(db),
        settings_repo=InstanceSettingsRepository(db),
    )


async def get_invite_service(
    db: AsyncSession = Depends(get_db),  # noqa: B008
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> InviteService:
    """Dependency that yields an initialised InviteService.

    Shares the request-scoped session and the wired ``AuthService`` (for
    token issuance on accept).  The email service is built from the SMTP
    settings like ``get_auth_service`` does.

    Args:
        db: Async DB session from dependency injection.
        auth_service: Fully wired auth service (token issuance).

    Returns:
        An initialised ``InviteService``.
    """
    email_config = EmailConfig.from_settings(get_settings())
    return InviteService(
        repo=UserRepository(db),
        auth_service=auth_service,
        email_service=EmailService(email_config),
    )


async def get_admin_invite_service(
    db: AsyncSession = Depends(get_db_admin),  # noqa: B008
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> InviteService:
    """Dependency that yields an InviteService on the RLS-bypass session.

    Admin-scoped analogue of :func:`get_invite_service` for the
    admin invite endpoints — the pending row belongs to the invitee, so
    the tenant session rejects the INSERT (RLS 500).  The pre-auth
    accept/info flow stays on :func:`get_invite_service`.

    Args:
        db: RLS-bypass async DB session from dependency injection.
        auth_service: Fully wired auth service (token issuance).

    Returns:
        An initialised ``InviteService`` on the bypass session.
    """
    email_config = EmailConfig.from_settings(get_settings())
    return InviteService(
        repo=UserRepository(db),
        auth_service=auth_service,
        email_service=EmailService(email_config),
    )


# ── User config (GET/PATCH /v1/me/config) ──────────────────────────────────


async def get_user_config_service(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> UserConfigService:
    """Dependency that yields an initialised UserConfigService.

    Args:
        request: Incoming HTTP request (for ``app.state`` access).
        db: Async DB session from dependency injection.

    Returns:
        An initialised ``UserConfigService``.
    """
    redis = getattr(request.app.state, "redis", None)
    return UserConfigService(
        user_repo=UserRepository(db),
        redis=redis,
        auth_repo=AuthRepository(db),
        db=db,
    )


# ── Fact ────────────────────────────────────────────────────────────────────────


async def get_fact_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
    webhook: WebhookService = Depends(get_webhook_service),
) -> FactService:
    """Dependency that yields an initialised FactService.

    Reads the Redis client from ``request.app.state.redis`` (initialised
    during the application lifespan).  Also passes a lazy per-user graph
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
    """Build a lazy per-user graph-backend resolver from ``request.app.state``.

    Reuses ``workers.backend.resolve_graph_backend`` — the same
    resolution used by the enrichment workers — so the API path and the
    worker path share one implementation (dispatcher, per-user config via
    DB cache-first, SurrealDB pool only when configured, FalkorDB
    client).  Resolution raises ``GraphBackendUnavailableError`` on
    failure; ``FactService.ingest_facts`` catches it and proceeds without
    graph sync (facts are the source of truth).

    Args:
        request: The HTTP request — ``app.state`` carries the graph
            collaborators registered during the lifespan.
        db: The request-scoped session (user-config queries and the
            Postgres backend bind).

    Returns:
        An async callable ``(user_id) -> GraphBackend | None``.
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
        "redis": getattr(request.app.state, "redis", None),
    }

    async def _resolve(user_id: UUID) -> GraphBackend | None:
        return await resolve_graph_backend(ctx, user_id, db)

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
        project_repo=ProjectRepository(db),
    )


# ── Graph ──────────────────────────────────────────────────────────────────────


async def get_graph_service(
    request: Request,
    platform_config: PlatformConfigBase = Depends(get_platform_config),
    db: AsyncSession = Depends(get_db),
    webhook: WebhookService = Depends(get_webhook_service),
) -> GraphService:
    """Dependency that yields an initialised GraphService.

    Uses the ``GraphBackendDispatcher`` (registered in the app lifespan)
    to resolve the per-user backend and create a request-scoped instance.
    Wires in the ``UserRepository`` for user-existence checks and
    ``FactRepository`` for session-scoped entity queries.
    """
    dispatcher: GraphBackendDispatcher = request.app.state.graph_backend_dispatcher

    # Resolve SurrealDB connection only when the user selects it.
    # For falkordb or none backends the pool is never touched — avoids
    # unnecessary network round-trips and failures when SurrealDB is down.
    # Connection details come solely from OZ_SURREALDB_* env vars (see
    # SurrealConnectionPool.get_or_create); a missing URL raises loudly.
    surreal = None
    user_id = UUID(request.state.user_id)
    if platform_config.graph_backend == "surrealdb":
        pool = request.app.state.surreal_connection_pool
        if pool is None:
            raise GraphBackendUnavailableError(
                f"SurrealDB configured for user {user_id} (graph_backend="
                "'surrealdb') but the surreal_connection_pool is missing."
            )
        try:
            surreal = await pool.get_or_create(user_id)
        except Exception as exc:
            logger.error(
                "graph_service.surreal_connection_failed",
                extra={
                    "user_id": str(user_id),
                    "backend": "surrealdb",
                    "error": str(exc),
                },
            )
            raise GraphBackendUnavailableError(
                f"SurrealDB connection failed for user {user_id} "
                f"with graph_backend='surrealdb': {exc}"
            ) from exc

    # The FalkorDB client is system-level (OZ_FALKORDB_URL, built at
    # lifespan) — no per-user override.
    falkordb_client = getattr(request.app.state, "falkordb_client", None)

    # FalkorDB selected but no client → fail loud.
    if platform_config.graph_backend == "falkordb" and falkordb_client is None:
        raise GraphBackendUnavailableError(
            f"FalkorDB configured for user {user_id} (graph_backend="
            "'falkordb') but no client is available — check OZ_FALKORDB_URL."
        )

    try:
        graph_backend = dispatcher.resolve_and_create(
            platform_config,
            db,
            surreal=surreal,
            falkordb_client=falkordb_client,
        )
    except GraphBackendUnavailableError:
        raise
    except ValueError as exc:
        # Unknown backend name for a configured user — misconfiguration, not
        # disabled.  Surface as 503 (ServiceUnavailable) rather than 500.
        logger.error(
            "graph_service.unknown_backend",
            extra={
                "user_id": str(user_id),
                "backend": platform_config.graph_backend,
            },
        )
        raise GraphBackendUnavailableError(
            f"Unknown graph backend '{platform_config.graph_backend}' for user "
            f"{user_id}: {exc}"
        ) from exc

    return GraphService(
        graph_backend=graph_backend,
        user_repo=UserRepository(db),
        fact_repo=FactRepository(db),
        webhook_service=webhook,
        episode_repo=EpisodeRepository(db),
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
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> QuickActionsService:
    """Dependency that yields an initialised QuickActionsService.

    Wires in the project, user, and organization repositories for
    context-aware action generation, plus the DB session and Redis client
    for canonical per-user config resolution (cache-first).
    """
    redis = getattr(request.app.state, "redis", None)
    return QuickActionsService(
        project_repo=ProjectRepository(db),
        user_repo=UserRepository(db),
        org_repo=OrganizationRepository(db),
        db=db,
        redis=redis,
    )


# ── Graph Backend (read-only) ────────────────────────────────────────────


async def get_graph_backend_for_project(
    request: Request,
    platform_config: PlatformConfigBase = Depends(get_platform_config),
    db: AsyncSession = Depends(get_db),
) -> GraphBackend:
    """Dependency that resolves and returns a project-scoped graph backend.

    This is a lighter alternative to ``get_graph_service`` for read-only
    queries that only need the backend (not the full ``GraphService``).
    The backend is resolved from the user configuration and returned
    directly — no service wrapping.

    Args:
        request: Incoming HTTP request (for ``app.state`` access).
        platform_config: The resolved user configuration including backend type.
        db: Async DB session (required by some backends).

    Returns:
        A ``GraphBackend`` instance configured for the current user.

    Raises:
        GraphBackendUnavailableError: If the selected backend is
            unreachable (e.g. SurrealDB connection fails), or if no
            graph backend is configured for this user (graph_backend
            unset or ``"none"``).
    """
    dispatcher: GraphBackendDispatcher = request.app.state.graph_backend_dispatcher

    surreal = None
    user_id = UUID(request.state.user_id)
    if platform_config.graph_backend == "surrealdb":
        pool = request.app.state.surreal_connection_pool
        if pool is None:
            raise GraphBackendUnavailableError(
                f"SurrealDB configured for user {user_id} (graph_backend="
                "'surrealdb') but the surreal_connection_pool is missing."
            )
        try:
            surreal = await pool.get_or_create(user_id)
        except Exception as exc:
            logger.error(
                "graph_backend.surreal_connection_failed",
                extra={
                    "user_id": str(user_id),
                    "backend": "surrealdb",
                    "error": str(exc),
                },
            )
            raise GraphBackendUnavailableError(
                f"SurrealDB connection failed for user {user_id}: {exc}"
            ) from exc

    falkordb_client = getattr(request.app.state, "falkordb_client", None)

    # Configured-but-unavailable guards — mirror get_graph_service so the
    # read-only path never constructs a doomed backend or 500s on an
    # unknown backend name.
    if platform_config.graph_backend == "falkordb" and falkordb_client is None:
        raise GraphBackendUnavailableError(
            f"FalkorDB configured for user {user_id} (graph_backend="
            "'falkordb') but no client is available — check "
            "OZ_FALKORDB_URL."
        )

    try:
        graph_backend = dispatcher.resolve_and_create(
            platform_config,
            db,
            surreal=surreal,
            falkordb_client=falkordb_client,
        )
    except GraphBackendUnavailableError:
        raise
    except ValueError as exc:
        logger.error(
            "graph_backend.unknown_backend",
            extra={
                "user_id": str(user_id),
                "backend": platform_config.graph_backend,
            },
        )
        raise GraphBackendUnavailableError(
            f"Unknown graph backend '{platform_config.graph_backend}' for user "
            f"{user_id}: {exc}"
        ) from exc

    # resolve_and_create returns None when graph_backend is unset or "none"
    # for this user — fail loud with a 503 instead of leaking a None backend
    # that 500s downstream callers (e.g. ObservationQueryService).
    if graph_backend is None:
        logger.error(
            "graph_backend.not_configured",
            extra={"user_id": str(user_id), "backend": platform_config.graph_backend},
        )
        raise GraphBackendUnavailableError(
            f"No graph backend is configured for user {user_id} "
            "(graph_backend is unset or 'none') — graph features are "
            "disabled for this user."
        )

    return graph_backend
