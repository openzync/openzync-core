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

import asyncio
from uuid import UUID

import orjson
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    Header,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import ValidationError

from core.audit import audit_action
from core.exceptions import (
    PayloadTooLargeError,
)
from core.exceptions import ValidationError as DomainValidationError
from dependencies.auth import get_current_user_id, require_permission
from dependencies.project_auth import require_project_membership
from dependencies.services import get_memory_service
from schemas.memory import (
    DeleteMemoryConfirm,
    IngestMemoryRequest,
    IngestMemoryResponse,
)
from services.idempotency_service import IdempotencyService
from services.memory_service import MemoryService

router = APIRouter(
    prefix="/v1/projects/{project_id}/memory",
    tags=["Memory"],
)

MAX_BLOB_FILES = 10
"""Max files per ingest — mirrors the dashboard playground cap."""

MAX_BLOB_TOTAL_BYTES = 200 * 1024 * 1024
"""Max combined upload bytes per ingest — mirrors the dashboard cap."""


def _measure_spooled_size(upload: UploadFile) -> int:
    """Return the size of an upload without consuming its stream.

    Runs in a worker thread — a disk-spooled temp file seek must not block
    the event loop.  The position is reset to 0 so downstream reads start
    from the beginning.
    """
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


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
        413: {"description": "Upload exceeds message, file, count, or total caps."},
        422: {"description": "Validation error (e.g., empty messages list, invalid blob refs)."},
    },
)
@audit_action("memory.ingest", "episode", "Messages ingested")
async def ingest_messages(
    request: Request,
    response: Response,
    data: str = Form(..., description="JSON payload: IngestMemoryRequest"),
    blobs: list[UploadFile] = File(default=[], description="Binary file attachments; send every file part under the field name 'blobs'"),
    service: MemoryService = Depends(get_memory_service),
    _: None = Depends(require_project_membership),
    _perm: None = Depends(require_permission("project:write")),
    created_by: UUID = Depends(get_current_user_id),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> IngestMemoryResponse:
    """Ingest messages into a project's memory.

    The request must be ``multipart/form-data`` (even for text-only calls).

    Form fields:
    - ``data``: A JSON-encoded ``IngestMemoryRequest`` containing
      ``session_id`` and ``messages`` array.  Each message can optionally
      include a ``blobs`` array referencing uploaded files by index.
    - ``blobs``: Binary file parts. Send every file under the field name
      ``blobs`` (repeat the name per file); files are matched to ``blob_id``
      refs by positional index. Parts under any other field name are ignored.

    Example::

        --boundary
        Content-Disposition: form-data; name="data"
        Content-Type: application/json

        {"session_id": "abc", "messages": [{"role": "user", "content": "See attached", "blobs": [{"blob_id": 0, "mime_type": "image/png", "file_name": "shot.png"}]}]}
        --boundary
        Content-Disposition: form-data; name="blobs"; filename="shot.png"
        Content-Type: image/png

        <binary>
        --boundary--

    Returns HTTP 202 with a ``Location`` header pointing to the job status
    endpoint: ``/v1/projects/{project_id}/memory/jobs/{job_id}``.  (A replay
    that lost its content-hash claim AND its prior job id — expired dedup
    row — returns 202 with the ingest committed but no ``Location``; the
    next identical ingest replays normally with a header.)
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])

    # Idempotency-Key is client-supplied — enforce the 255-char ceiling at
    # the boundary so an oversized key is a 422, not a 500 propagated from
    # IdempotencyService's internal ValueError (defense-in-depth).
    if idempotency_key is not None and len(idempotency_key) > 255:
        raise DomainValidationError(
            "Idempotency-Key must not exceed 255 characters",
        )

    # Server-side upload caps (mirror the dashboard playground constants):
    # per-file 50MB is enforced in the service via read_upload_bounded.
    if len(blobs) > MAX_BLOB_FILES:
        raise PayloadTooLargeError(
            f"Too many files: {len(blobs)} uploaded, "
            f"maximum is {MAX_BLOB_FILES} per ingest",
        )
    total_bytes = 0
    for blob in blobs:
        size = blob.size
        if size is None:
            size = await asyncio.to_thread(_measure_spooled_size, blob)
        total_bytes += size
    if total_bytes > MAX_BLOB_TOTAL_BYTES:
        raise PayloadTooLargeError(
            f"Attachments total {total_bytes} bytes, exceeding the "
            f"{MAX_BLOB_TOTAL_BYTES}-byte limit per ingest",
        )

    # Parse the JSON payload from the multipart form
    try:
        payload = IngestMemoryRequest.model_validate_json(data)
    except ValidationError as exc:
        # ctx["error"] holds the raw ValueError instance (not JSON
        # serializable) — str() it or the RFC 7807 renderer 500s on this 422.
        errors = exc.errors()
        for error in errors:
            ctx = error.get("ctx")
            if ctx and isinstance(ctx.get("error"), Exception):
                ctx["error"] = str(ctx["error"])
        raise DomainValidationError(
            "Invalid request payload",
            detail={"errors": errors},
        ) from exc

    # Validate blob reference integrity
    referenced_ids: set[int] = set()
    for msg in payload.messages:
        for bm in msg.blobs:
            if bm.blob_id < 0:
                raise DomainValidationError(
                    f"Invalid blob_id {bm.blob_id}: must be non-negative",
                )
            referenced_ids.add(bm.blob_id)

    if blobs:
        # Every referenced blob must have a corresponding uploaded file
        max_allowed = len(blobs) - 1
        if any(bid > max_allowed for bid in referenced_ids):
            raise DomainValidationError(
                f"blob_id out of range: max is {max_allowed} "
                f"but referenced IDs include {referenced_ids}",
            )

        # Every uploaded file must be referenced by at least one message
        for i, _ in enumerate(blobs):
            if i not in referenced_ids:
                raise DomainValidationError(
                    f"Uploaded file blob_{i} is not referenced by any message",
                )

    # Canonical body hash — only needed when an Idempotency-Key is present.
    # The RAW form string is hashed, not the validated model: Pydantic
    # applies defaults, so two equivalent-but-differently-shaped bodies
    # would hash differently after validation and break the 409 contract.
    # The double JSON parse (validation + hashing) is intentional.
    body_hash = (
        IdempotencyService.hash_request_body(orjson.loads(data))
        if idempotency_key
        else None
    )

    result = await service.ingest(
        org_id=org_id,
        project_id=project_id,
        created_by=created_by,
        session_external_id=payload.session_id,
        messages=payload.messages,
        uploaded_blobs=blobs,
        idempotency_key=idempotency_key,
        body_hash=body_hash,
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
    description="Soft-delete all episodes and facts for a project. Requires "
    "a JSON body ``{\"confirm\": \"<project_id>\"}`` matching the path "
    "project — any other value is rejected with 422. This is "
    "the data wipe operation — all sessions are preserved, but all message "
    "history and extracted facts are invalidated.",
    responses={
        204: {"description": "Memory deleted successfully (no content)."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        422: {"description": "Confirm body missing or not matching the project ID."},
    },
)
@audit_action("memory.wipe", "memory", "Memory wiped")
async def delete_project_memory(
    request: Request,
    body: DeleteMemoryConfirm = Body(...),
    service: MemoryService = Depends(get_memory_service),
    _: None = Depends(require_project_membership),
    _perm: None = Depends(require_permission("project:write")),
    actor: UUID = Depends(get_current_user_id),
) -> Response:
    """Delete all memory for a project.

    Soft-deletes all episodes (messages) and facts for the given project.
    Sessions remain intact. This operation is **not** reversible — deleted
    data is marked as inactive but preserved for a 30-day GDPR grace period
    before hard-purge. The ``confirm`` body must echo the path project ID.
    """
    # note: explicit empty Response — implicit None makes FastAPI serialize
    # a JSON `null` body on the 204, which uvicorn rejects and rolls back
    # the wipe in get_db teardown after 204 headers flush.
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])
    await service.delete_project_memory(
        org_id=org_id,
        project_id=project_id,
        confirm=body.confirm,
        actor_id=actor,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
