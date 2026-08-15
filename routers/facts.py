"""Business data ingestion endpoint — HTTP adapter layer only.

Provides:
- ``POST /v1/projects/{project_id}/facts`` — Ingest a batch of fact triples
  into a project's knowledge graph. Returns 202 with a job_id for tracking.
- ``GET /v1/projects/{project_id}/facts`` — List facts valid at a point in
  time (default now) so supersession/invalidation is observable.

Every handler is a thin adapter that:
1. Extracts input from the request (path params, query params, body).
2. Calls the service/repository layer.
3. Returns a Pydantic response with appropriate HTTP status code.

No business logic. No database queries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import audit_action
from dependencies.auth import get_current_user_id
from dependencies.db import get_db
from dependencies.project_auth import require_project_membership
from dependencies.services import get_fact_service
from repositories.fact_repository import FactRepository
from schemas.facts import (
    FactBatchRequest,
    FactBatchResponse,
    FactHistoryResponse,
    FactResponse,
    FactRetractRequest,
    PaginatedFactsResponse,
)
from services.fact_service import FactService

router = APIRouter(
    prefix="/v1/projects/{project_id}/facts",
    tags=["Facts"],
)


async def _get_fact_repository(db: AsyncSession = Depends(get_db)) -> FactRepository:
    """Dependency that yields a request-scoped FactRepository.

    Follows the router-local factory pattern used by ``routers/projects.py``
    and ``routers/admin_schemas.py`` — no service wraps the read primitive.
    """
    return FactRepository(db)


# ── POST: Ingest business facts ──────────────────────────────────────────────


@router.post(
    "",
    status_code=202,
    response_model=FactBatchResponse,
    summary="Ingest business fact triples",
    description="Ingest a batch of fact triples (subject-predicate-object) "
    "into a project's knowledge graph. Facts are persisted in PostgreSQL and "
    "embedding tasks are enqueued asynchronously. Returns 202 immediately "
    "with a job_id for tracking. Maximum 500 triples per request.",
    responses={
        202: {"description": "Accepted — facts queued for processing."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        422: {"description": "Validation error (e.g., empty batch, >500 triples, "
            "invalid triple format)."},
    },
)
@audit_action("fact.create", "fact", "Fact created")
async def ingest_facts(
    request: Request,
    payload: FactBatchRequest,
    service: FactService = Depends(get_fact_service),
    _: None = Depends(require_project_membership),
    created_by: UUID = Depends(get_current_user_id),
) -> FactBatchResponse:
    """Ingest a batch of fact triples into a project's knowledge graph.

    - ``session_id`` is required. Facts are associated with the specified
      session, which must already exist.
    - Maximum 500 fact triples per request (enforced by schema validation).
    - Each triple requires ``subject``, ``predicate``, and ``object``.
      ``content`` is auto-generated if omitted.
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])
    return await service.ingest_facts(
        org_id=org_id,
        project_id=project_id,
        created_by=created_by,
        facts=payload.facts,
        session_external_id=payload.session_id,
    )


# ── GET: List facts valid at a point in time ─────────────────────────────────


@router.get(
    "",
    response_model=PaginatedFactsResponse,
    summary="List facts valid at a point in time",
    description="List facts whose validity range contains the as-of "
    "timestamp (default now). Superseded facts (valid_to set) and "
    "explicitly retracted facts (invalid_at set) are excluded, so this "
    "endpoint makes supersession and invalidation observable.",
    responses={
        200: {"description": "Facts valid at the requested time."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        422: {"description": "Validation error (e.g., invalid as_of)."},
    },
)
async def list_facts_at_time(
    request: Request,
    as_of: datetime | None = Query(
        default=None,
        description="Effective-at timestamp (ISO-8601, UTC). Facts valid at "
        "this instant are returned. Defaults to now.",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum facts per page (1–200).",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of facts to skip (offset pagination).",
    ),
    repo: FactRepository = Depends(_get_fact_repository),
    _: None = Depends(require_project_membership),
) -> PaginatedFactsResponse:
    """List facts valid at an effective-at timestamp.

    Reads through ``FactRepository.get_facts_at_time`` — the single
    repository primitive that honors the effective-at predicate, so this
    endpoint automatically reflects supersession semantics.

    Args:
        request: The FastAPI request object (org/project IDs).
        as_of: Effective-at timestamp; ``None`` resolves to now.
        limit: Max facts per page.
        offset: Pagination offset.
        repo: Request-scoped FactRepository (injected).
        _: Project membership gate (injected).

    Returns:
        A ``PaginatedFactsResponse`` with facts valid at ``as_of``.
    """
    project_id = UUID(request.path_params["project_id"])
    org_id = UUID(request.state.org_id)

    # Default to now; coerce naive ISO-8601 input to UTC-aware so the
    # comparison against tz-aware DB timestamps is well-defined.
    query_time = as_of if as_of is not None else datetime.now(timezone.utc)
    if query_time.tzinfo is None:
        query_time = query_time.replace(tzinfo=timezone.utc)

    # Fetch limit+1 to detect whether another page exists (offset pagination).
    facts = await repo.get_facts_at_time(
        project_id=project_id,
        timestamp=query_time,
        organization_id=org_id,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(facts) > limit
    page = facts[:limit]

    return PaginatedFactsResponse(
        data=[FactResponse.model_validate(fact) for fact in page],
        next_cursor=str(offset + limit) if has_more else None,
        has_more=has_more,
    )


# ── POST: Retract a fact ────────────────────────────────────────────────────


@router.post(
    "/{fact_id}/retract",
    status_code=200,
    response_model=FactResponse,
    summary="Retract a fact",
    description="Hard-retract a fact by setting its invalid_at timestamp. "
    "Records a 'retracted' invalidation-lineage event and queues "
    "post-commit graph edge expiry, webhook emission, and cache purge. "
    "Idempotent — retracting an already-closed fact is a 200 no-op.",
    responses={
        200: {"description": "Fact retracted (or already closed — no-op)."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        404: {"description": "Fact not found."},
    },
)
async def retract_fact(
    request: Request,
    fact_id: UUID,
    payload: FactRetractRequest | None = None,
    service: FactService = Depends(get_fact_service),  # noqa: B008
    _: None = Depends(require_project_membership),  # noqa: B008
) -> FactResponse:
    """Retract a fact, recording its invalidation lineage.

    A fact closed by supersession (``valid_to`` set) or a previous
    retraction (``invalid_at`` set) is returned unchanged — the endpoint
    is idempotent.  The fact must belong to the project in the path;
    otherwise a 404 is returned (existence is not leaked across
    projects).

    Args:
        request: The FastAPI request object (org/project IDs).
        fact_id: The fact to retract.
        payload: Optional retraction metadata (reason).
        service: Request-scoped FactService (injected).
        _: Project membership gate (injected).

    Returns:
        The retracted fact (or the unchanged fact if already closed).
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])
    fact = await service.retract_fact(
        fact_id,
        organization_id=org_id,
        project_id=project_id,
        reason=payload.reason if payload else None,
    )
    return FactResponse.model_validate(fact)


# ── GET: Fact invalidation history ──────────────────────────────────────────


@router.get(
    "/{fact_id}/history",
    response_model=FactHistoryResponse,
    summary="Get a fact and its invalidation history",
    description="Return a fact plus its invalidation-lineage events "
    "(supersessions, retractions), newest first.",
    responses={
        200: {"description": "Fact and its lineage events."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        404: {"description": "Fact not found."},
    },
)
async def get_fact_history(
    request: Request,
    fact_id: UUID,
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum events per page (1–200).",
    ),  # noqa: B008
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of events to skip (offset pagination).",
    ),  # noqa: B008
    service: FactService = Depends(get_fact_service),  # noqa: B008
    _: None = Depends(require_project_membership),  # noqa: B008
) -> FactHistoryResponse:
    """Fetch a fact and its invalidation lineage.

    The fact must belong to the project in the path; otherwise a 404 is
    returned (existence is not leaked across projects).

    Args:
        request: The FastAPI request object (org/project IDs).
        fact_id: The fact whose lineage to fetch.
        limit: Max events per page (1–200).
        offset: Number of events to skip.
        service: Request-scoped FactService (injected).
        _: Project membership gate (injected).

    Returns:
        A ``FactHistoryResponse`` with the serialized fact and its
        lineage events (newest first).
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])
    result = await service.get_fact_history(
        fact_id,
        organization_id=org_id,
        project_id=project_id,
        limit=limit,
        offset=offset,
    )
    return FactHistoryResponse(fact=result["fact"], events=result["events"])
