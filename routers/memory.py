"""Memory ingestion and management endpoints — HTTP adapter layer only.

Provides two endpoints:
- ``POST /v1/users/{user_id}/memory`` — ingest messages into a user's memory.
  Returns 202 with a ``Location`` header pointing to the job status endpoint.
- ``DELETE /v1/users/{user_id}/memory`` — wipe all memory for a user
  (soft-delete episodes + facts). Returns 204.

Every handler is a thin adapter that:
1. Extracts input from the request (path params, headers, body).
2. Calls the service layer.
3. Returns a Pydantic response with appropriate HTTP status code.

No business logic. No database queries.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_org_id
from dependencies.db import get_db
from repositories.episode_repository import EpisodeRepository
from repositories.fact_repository import FactRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from schemas.memory import IngestMemoryRequest, IngestMemoryResponse
from services.memory_service import MemoryService

router = APIRouter(prefix="/v1/users/{user_id}/memory", tags=["Memory"])


# ── Dependency factory ───────────────────────────────────────────────────────


async def get_memory_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> MemoryService:
    """FastAPI dependency that yields an initialised :class:`MemoryService`.

    Wires up repositories and Redis with the request-scoped DB session.
    The Redis client is read from ``request.app.state.redis_client``
    (initialised during the application lifespan).
    """
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None:
        raise RuntimeError(
            "redis_client not found on app.state. "
            "Ensure init_redis() was called during the application lifespan."
        )

    return MemoryService(
        db=db,
        redis_client=redis_client,
        episode_repo=EpisodeRepository(db),
        session_repo=SessionRepository(db),
        user_repo=UserRepository(db),
        fact_repo=FactRepository(db),
    )


# ── POST: Ingest messages ────────────────────────────────────────────────────


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestMemoryResponse,
    summary="Ingest messages into user memory",
    description="Ingest conversation messages for a user. Messages are "
    "persisted as episodes in PostgreSQL and enrichment tasks are enqueued "
    "asynchronously. Returns 202 immediately with a Location header for "
    "job status tracking.",
    responses={
        202: {"description": "Accepted — messages queued for processing."},
        401: {"description": "Missing or invalid authentication."},
        404: {"description": "User not found in organization."},
        413: {"description": "Content exceeds 64KB limit per message."},
        422: {"description": "Validation error (e.g., empty messages list)."},
    },
)
async def ingest_messages(
    user_id: UUID,
    payload: IngestMemoryRequest,
    response: Response,
    org_id: str = Depends(require_org_id),
    service: MemoryService = Depends(get_memory_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> IngestMemoryResponse:
    """Ingest messages into a user's memory.

    - ``session_id`` is optional. If omitted, a ``__default__`` session
      is auto-created for the user.
    - Provide an ``Idempotency-Key`` header to make the request idempotent
      (cached for 48 hours). A duplicate key with the same payload returns
      the same response without re-processing.
    - Each message ``content`` is limited to 64KB (UTF-8 bytes).

    Returns HTTP 202 with a ``Location`` header pointing to the job status
    endpoint: ``/v1/users/{user_id}/memory/jobs/{job_id}``.
    """
    result = await service.ingest(
        org_id=UUID(org_id),
        user_uuid=user_id,
        session_external_id=payload.session_id,
        messages=payload.messages,
        idempotency_key=idempotency_key,
    )

    # Set Location header for job status tracking
    if result.job_id is not None:
        response.headers["Location"] = (
            f"/v1/users/{user_id}/memory/jobs/{result.job_id}"
        )

    return result


# ── DELETE: Wipe user memory ────────────────────────────────────────────────


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete all user memory",
    description="Soft-delete all episodes and facts for a user. This is "
    "the GDPR / memory-wipe operation — the user and their sessions are "
    "preserved, but all message history and extracted facts are "
    "invalidated.",
    responses={
        204: {"description": "Memory deleted successfully (no content)."},
        401: {"description": "Missing or invalid authentication."},
        404: {"description": "User not found in organization."},
    },
)
async def delete_user_memory(
    user_id: UUID,
    org_id: str = Depends(require_org_id),
    service: MemoryService = Depends(get_memory_service),
) -> None:
    """Delete all memory for a user.

    Soft-deletes all episodes (messages) and facts for the given user.
    The user and their sessions remain intact. This operation is **not**
    reversible — deleted data is marked as inactive but preserved for
    a 30-day GDPR grace period before hard-purge.
    """
    await service.delete_user_memory(
        org_id=UUID(org_id),
        user_uuid=user_id,
    )
