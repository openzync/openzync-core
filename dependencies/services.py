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

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.db import get_db
from middleware.auth_throttle import AuthThrottle
from repositories.auth_repository import AuthRepository
from repositories.episode_repository import EpisodeRepository
from repositories.fact_repository import FactRepository
from repositories.organization_repository import OrganizationRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from services.auth_service import AuthService
from services.fact_service import FactService
from services.graph_service import GraphService
from services.memory_service import MemoryService
from services.session_service import SessionService
from services.user_service import UserService


# ── User ───────────────────────────────────────────────────────────────────────


async def get_user_service(
    db: AsyncSession = Depends(get_db),
) -> UserService:
    """Dependency that yields an initialised UserService.

    The service is constructed once per request using a DB session from
    the application's async engine.
    """
    return UserService(repo=UserRepository(db))


# ── Session ────────────────────────────────────────────────────────────────────


async def get_session_service(
    db: AsyncSession = Depends(get_db),
) -> SessionService:
    """Dependency that yields an initialised SessionService.

    The service is constructed once per request using a DB session from
    the application's async engine.
    """
    return SessionService(repo=SessionRepository(db))


# ── Auth ───────────────────────────────────────────────────────────────────────


async def get_auth_service(
    db: AsyncSession = Depends(get_db),
) -> AuthService:
    """Dependency that yields an initialised AuthService."""
    return AuthService(repo=AuthRepository(db))


# ── Fact ────────────────────────────────────────────────────────────────────────


async def get_fact_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> FactService:
    """Dependency that yields an initialised FactService.

    Reads the Redis client from ``request.app.state.redis`` (initialised
    during the application lifespan).
    """
    redis_client = getattr(request.app.state, "redis", None)
    return FactService(
        db=db,
        redis_client=redis_client,
        fact_repo=FactRepository(db),
        user_repo=UserRepository(db),
        session_repo=SessionRepository(db),
    )


# ── Memory ─────────────────────────────────────────────────────────────────────


async def get_memory_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
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
    )


# ── Graph ──────────────────────────────────────────────────────────────────────


async def get_graph_service(
    db: AsyncSession = Depends(get_db),
) -> GraphService:
    """Dependency that yields an initialised GraphService.

    Creates a request-scoped ``PostgresGraphBackend`` and wires in the
    ``UserRepository`` for user-existence checks.
    """
    from packages.graphiti_client.backends.postgres import PostgresGraphBackend

    graph_backend = PostgresGraphBackend(db=db)
    return GraphService(
        graph_backend=graph_backend,
        user_repo=UserRepository(db),
    )


# ── Auth Throttle ─────────────────────────────────────────────────────────────


async def get_auth_throttle(
    request: Request,
) -> AuthThrottle:
    """Dependency that yields an initialised AuthThrottle.

    Reads the Redis client from ``request.app.state.redis``.
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise RuntimeError(
            "Redis client not found on app.state. "
            "Ensure init_redis() was called during the application lifespan."
        )
    return AuthThrottle(redis)
