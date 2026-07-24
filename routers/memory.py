"""Memory ingestion and management endpoints — HTTP adapter layer only.

Provides two endpoints:
- ``POST /v1/projects/{project_id}/memory`` — ingest messages with optional
  file attachments via multipart form-data.  Returns 202 with a ``Location``
  header pointing to the job status endpoint.
- ``DELETE /v1/projects/{project_id}/memory`` — wipe all memory for a
  project (soft-delete episodes + facts). Returns 204.

Every handler is a thin adapter that:
1. Extracts input from the request (path params, headers, form fields, files).
2. Calls the service layer.
3. Returns a Pydantic response with appropriate HTTP status code.

No business logic. No database queries.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile, status

from dependencies.auth import get_current_user_id
from dependencies.project_auth import require_project_membership
from dependencies.services import get_memory_service
from schemas.memory import IngestMemoryRequest, IngestMemoryResponse
from services.memory_service import MemoryService

router = APIRouter(
    prefix="/v1/projects/{project_id}/memory",
    tags=["Memory"],
)


# ── POST: Ingest messages with optional blobs ────────────────────────────────


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestMemoryResponse,
    summary="Ingest messages into project memory",
    description="Ingest conversation messages for a project. Messages are "
    "persisted as episodes in PostgreSQL and enrichment tasks are enqueued "
    "asynchronously.  Supports optional file attachments (images, PDFs, "
    "documents) via multipart form-data.  Returns 202 immediately with a "
    "Location header for job status tracking.",
    responses={
        202: {"description": "Accepted — messages queued for processing."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        413: {"description": "Content exceeds 64KB limit per message or blob size limit."},
        422: {"description": "Validation error (e.g., empty messages list, invalid blob refs)."},
    },
)
async def ingest_messages(
    request: Request,
    response: Response,
    data: str = Form(..., description="JSON payload: IngestMemoryRequest"),
    blobs: list[UploadFile] = File(default=[], description="Binary file attachments (blob_0, blob_1, ...)"),
    service: MemoryService = Depends(get_memory_service),
    _: None = Depends(require_project_membership),
    created_by: UUID = Depends(get_current_user_id),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> IngestMemoryResponse:
    """Ingest messages into a project's memory.

    The request must be ``multipart/form-data`` (even for text-only calls).

    Form fields:
    - ``data``: A JSON-encoded ``IngestMemoryRequest`` containing
      ``session_id`` and ``messages`` array.  Each message can optionally
      include a ``blobs`` array referencing uploaded files by index.
    - ``blob_0``, ``blob_1``, ...: Binary file fields.  The field names
      are not significant — files are matched by positional index.

    Example::

        --boundary
        Content-Disposition: form-data; name="data"
        Content-Type: application/json

        {"session_id": "abc", "messages": [{"role": "user", "content": "See attached", "blobs": [{"blob_id": 0, "mime_type": "image/png", "file_name": "shot.png"}]}]}
        --boundary
        Content-Disposition: form-data; name="blob_0"; filename="shot.png"
        Content-Type: image/png

        <binary>
        --boundary--

    Returns HTTP 202 with a ``Location`` header pointing to the job status
    endpoint: ``/v1/projects/{project_id}/memory/jobs/{job_id}``.
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])

    # Parse the JSON payload from the multipart form
    payload = IngestMemoryRequest.model_validate_json(data)

    # Validate that every blob_id referenced in messages has a matching upload
    max_referenced = -1
    for msg in payload.messages:
        for blob_ref in msg.blobs:
            if blob_ref.blob_id > max_referenced:
                max_referenced = blob_ref.blob_id
    if max_referenced >= 0 and len(blobs) <= max_referenced:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Message references blob_id={max_referenced} but only "
                f"{len(blobs)} file(s) were uploaded.  Ensure every "
                "referenced blob has a corresponding multipart file field."
            )
        )

    result = await service.ingest(
        org_id=org_id,
        project_id=project_id,
        created_by=created_by,
        session_external_id=payload.session_id,
        messages=payload.messages,
        uploaded_blobs=blobs,
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
