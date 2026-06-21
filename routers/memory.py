"""Memory ingestion and management endpoints — HTTP adapter layer only.

Provides two endpoints:
- ``POST /v1/projects/{project_id}/memory`` — ingest messages into a
  project's memory.  Returns 202 with a ``Location`` header pointing to
  the job status endpoint.
- ``DELETE /v1/projects/{project_id}/memory`` — wipe all memory for a
  project (soft-delete episodes + facts). Returns 204.

Every handler is a thin adapter that:
1. Extracts input from the request (path params, headers, body).
2. Calls the service layer.
3. Returns a Pydantic response with appropriate HTTP status code.

No business logic. No database queries.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status

from dependencies.auth import get_current_user_id
from dependencies.project_auth import require_project_membership
from dependencies.services import get_memory_service
from schemas.memory import IngestMemoryRequest, IngestMemoryResponse
from services.memory_service import MemoryService

router = APIRouter(
    prefix="/v1/projects/{project_id}/memory",
    tags=["Memory"],
)


# ── POST: Ingest messages ────────────────────────────────────────────────────


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestMemoryResponse,
    summary="Ingest messages into project memory",
    description="Ingest conversation messages for a project. Messages are "
    "persisted as episodes in PostgreSQL and enrichment tasks are enqueued "
    "asynchronously. Returns 202 immediately with a Location header for "
    "job status tracking.",
    responses={
        202: {"description": "Accepted — messages queued for processing."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        413: {"description": "Content exceeds 64KB limit per message."},
        422: {"description": "Validation error (e.g., empty messages list)."},
    },
)
async def ingest_messages(
    request: Request,
    payload: IngestMemoryRequest,
    response: Response,
    service: MemoryService = Depends(get_memory_service),
    _: None = Depends(require_project_membership),
    created_by: UUID = Depends(get_current_user_id),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> IngestMemoryResponse:
    """Ingest messages into a project's memory.

    - ``session_id`` is optional. If omitted, a ``__default__`` session
      is auto-created for the project.
    - Provide an ``Idempotency-Key`` header to make the request idempotent
      (cached for 48 hours). A duplicate key with the same payload returns
      the same response without re-processing.
    - Each message ``content`` is limited to 64KB (UTF-8 bytes).

    Returns HTTP 202 with a ``Location`` header pointing to the job status
    endpoint: ``/v1/projects/{project_id}/memory/jobs/{job_id}``.
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])

    result = await service.ingest(
        org_id=org_id,
        project_id=project_id,
        created_by=created_by,
        session_external_id=payload.session_id,
        messages=payload.messages,
        idempotency_key=idempotency_key,
    )

    # Set Location header for job status tracking
    if result.job_id is not None:
        response.headers["Location"] = (
            f"/v1/projects/{project_id}/memory/jobs/{result.job_id}"
        )

    return result


# ── DELETE: Wipe project memory ──────────────────────────────────────────────


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete all project memory",
    description="Soft-delete all episodes and facts for a project. This is "
    "the data wipe operation — all sessions are preserved, but all message "
    "history and extracted facts are invalidated.",
    responses={
        204: {"description": "Memory deleted successfully (no content)."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
    },
)
async def delete_project_memory(
    request: Request,
    service: MemoryService = Depends(get_memory_service),
    _: None = Depends(require_project_membership),
) -> None:
    """Delete all memory for a project.

    Soft-deletes all episodes (messages) and facts for the given project.
    Sessions remain intact. This operation is **not** reversible — deleted
    data is marked as inactive but preserved for a 30-day GDPR grace period
    before hard-purge.
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])
    await service.delete_project_memory(
        org_id=org_id,
        project_id=project_id,
    )
