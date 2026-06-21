"""Business data ingestion endpoint — HTTP adapter layer only.

Provides:
- ``POST /v1/projects/{project_id}/facts`` — Ingest a batch of fact triples
  into a project's knowledge graph. Returns 202 with a job_id for tracking.

Every handler is a thin adapter that:
1. Extracts input from the request (path params, body).
2. Calls the service layer.
3. Returns a Pydantic response with appropriate HTTP status code.

No business logic. No database queries.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from dependencies.auth import get_current_user_id
from dependencies.project_auth import require_project_membership
from dependencies.services import get_fact_service
from schemas.facts import FactBatchRequest, FactBatchResponse
from services.fact_service import FactService

router = APIRouter(
    prefix="/v1/projects/{project_id}/facts",
    tags=["Facts"],
)


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
async def ingest_facts(
    request: Request,
    payload: FactBatchRequest,
    service: FactService = Depends(get_fact_service),
    _: None = Depends(require_project_membership),
    created_by: UUID = Depends(get_current_user_id),
) -> FactBatchResponse:
    """Ingest a batch of fact triples into a project's knowledge graph.

    - ``session_id`` is optional. If provided, facts are associated with
      the specified session.
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
