"""Business data ingestion endpoint — HTTP adapter layer only.

Provides:
- ``POST /v1/users/{user_id}/facts`` — Ingest a batch of fact triples
  into a user's knowledge graph. Returns 202 with a job_id for tracking.

Every handler is a thin adapter that:
1. Extracts input from the request (path params, body).
2. Calls the service layer.
3. Returns a Pydantic response with appropriate HTTP status code.

No business logic. No database queries.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_org_id
from dependencies.db import get_db
from repositories.fact_repository import FactRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from schemas.facts import FactBatchRequest, FactBatchResponse
from services.fact_service import FactService

router = APIRouter(prefix="/v1/users/{user_id}/facts", tags=["Facts"])


# ── Dependency factory ───────────────────────────────────────────────────────


async def get_fact_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> FactService:
    """FastAPI dependency that yields an initialised :class:`FactService`.

    Wires up repositories and Redis with the request-scoped DB session.
    The Redis client is read from ``request.app.state.redis``
    (initialised during the application lifespan).
    """
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        raise RuntimeError(
            "Redis client not found on app.state. "
            "Ensure init_redis() was called during the application lifespan."
        )

    return FactService(
        db=db,
        redis_client=redis_client,
        fact_repo=FactRepository(db),
        user_repo=UserRepository(db),
        session_repo=SessionRepository(db),
    )


# ── POST: Ingest business facts ──────────────────────────────────────────────


@router.post(
    "",
    status_code=202,
    response_model=FactBatchResponse,
    summary="Ingest business fact triples",
    description="Ingest a batch of fact triples (subject-predicate-object) "
    "into the user's knowledge graph. Facts are persisted in PostgreSQL and "
    "embedding tasks are enqueued asynchronously. Returns 202 immediately "
    "with a job_id for tracking. Maximum 500 triples per request.",
    responses={
        202: {"description": "Accepted — facts queued for processing."},
        401: {"description": "Missing or invalid authentication."},
        404: {"description": "User not found in organization."},
        422: {"description": "Validation error (e.g., empty batch, >500 triples, "
            "invalid triple format)."},
    },
)
async def ingest_facts(
    user_id: UUID,
    payload: FactBatchRequest,
    org_id: str = Depends(require_org_id),
    service: FactService = Depends(get_fact_service),
) -> FactBatchResponse:
    """Ingest a batch of fact triples into a user's knowledge graph.

    - ``session_id`` is optional. If provided, facts are associated with
      the specified session.
    - Maximum 500 fact triples per request (enforced by schema validation).
    - Each triple requires ``subject``, ``predicate``, and ``object``.
      ``content`` is auto-generated if omitted.
    """
    return await service.ingest_facts(
        org_id=UUID(org_id),
        user_uuid=user_id,
        facts=payload.facts,
        session_external_id=payload.session_id,
    )
