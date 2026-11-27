"""User CRUD endpoints — HTTP adapter layer only.

Every handler is a thin adapter that:
1. Extracts input from the request (path params, query params, body)
2. Calls the service layer
3. Returns a Pydantic response

No business logic. No database queries.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002 (runtime import — FastAPI resolves annotation names)
)
from starlette.responses import Response

from core.audit import audit_action
from core.exceptions import RateLimitError, ValidationError
from core.rls import rls_bypass
from dependencies.auth import (
    _ensure_admin,
    get_actor_permissions,
    require_admin,
    require_admin_or_self,
)
from dependencies.db import get_db
from dependencies.services import (
    get_admin_user_service,
    get_user_service,
    get_user_summary_service,
)
from schemas.custom_instructions import (
    CustomInstructionSchema,
    CustomInstructionsResponse,
    SetCustomInstructionsRequest,
)
from schemas.sorting import SortDir, SortSpec, UserSortBy
from schemas.user_summary import UserSummaryResponse, UserSummaryTriggerResponse
from schemas.users import (
    CreateUserRequest,
    UpdateUserRequest,
    UserListResponse,
    UserResponse,
    UserResponseWithStats,
)
from services.user_service import UserService
from services.user_summary_service import UserSummaryService

router = APIRouter(prefix="/v1/users", tags=["Users"])


@router.post("", response_model=UserResponse, status_code=201)
@audit_action("user.create", "user", "User created")
async def create_user(
    body: CreateUserRequest,
    user_id: str = Depends(require_admin),
    service: UserService = Depends(get_admin_user_service),
) -> UserResponse:
    """Create a new user.

    The ``external_id`` is caller-defined and must be unique within the
    organization. Returns 409 if a user with this ``external_id`` already
    exists.  Admin-gated (JWT platform admin only).
    """
    return await service.create_user(
        user_id=UUID(user_id),
        external_id=body.external_id,
        name=body.name,
        email=body.email,
        metadata=body.metadata,
        role=body.role,
    )


@router.get("", response_model=UserListResponse)
async def list_users(
    # ⚠️ BREAKING: listing members now requires the platform admin role —
    # previously any principal with ``members:read`` could enumerate the org.
    user_id: str = Depends(require_admin),
    service: UserService = Depends(get_admin_user_service),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Max results per page (1-200).",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque pagination cursor from previous response.",
    ),
    search: str | None = Query(
        default=None,
        min_length=1,
        max_length=256,
        description="Search external_id, name, email, and metadata.",
    ),
    created_after: datetime | None = Query(
        default=None,
        description="Only users created on or after this ISO-8601 timestamp.",
    ),
    created_before: datetime | None = Query(
        default=None,
        description="Only users created before this ISO-8601 timestamp.",
    ),
    sort_by: UserSortBy | None = Query(
        default=None,
        description="Sort key (default created_at).",
    ),
    sort_dir: SortDir = Query(
        default="desc",
        description="Sort direction (default desc).",
    ),
) -> UserListResponse:
    """List users with pagination and search.

    Supports cursor-based pagination, multi-field search, and date-range
    filtering. All filters are composable. Cursors encode sort — a cursor
    from a different sort fails with 422.
    """
    return await service.list_users(
        user_id=UUID(user_id),
        limit=limit,
        cursor=cursor,
        search=search,
        created_after=created_after,
        created_before=created_before,
        sort=SortSpec(sort_by=sort_by, sort_dir=sort_dir),
    )


@router.get("/{user_id}", response_model=UserResponseWithStats)
async def get_user(
    user_id: UUID,
    request: Request,
    service: UserService = Depends(get_user_service),
    # ⚠️ BREAKING: reading another member's record now requires the platform
    # admin role (role collapse — ``superadmin`` is gone); a JWT user may
    # still fetch their own record.
    _viewer_id: str = Depends(require_admin_or_self()),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> UserResponseWithStats:
    """Get a user by internal UUID.

    Returns profile information plus aggregate statistics
    (message_count, fact_count, session_count).
    """
    # note: tenant session for self-reads; an admin reading another
    # member's row runs under a bypass (tenant policy would hide it).
    # Only 403 (non-admin) is absorbed, 503 fails closed.
    try:
        await _ensure_admin(request, _viewer_id, db)
        is_admin = True
    except HTTPException as exc:
        if exc.status_code != status.HTTP_403_FORBIDDEN:
            raise
        is_admin = False
    if is_admin:
        async with rls_bypass(db):
            return await service.get_user(user_id)
    return await service.get_user(user_id)


@router.patch("/{user_id}", response_model=UserResponse)
@audit_action("user.update", "user", "User updated")
async def update_user(
    user_id: UUID,
    body: UpdateUserRequest,
    request: Request,
    service: UserService = Depends(get_user_service),
    _caller_id: str = Depends(require_admin_or_self()),  # noqa: B008
    actor_permissions: frozenset[str] | None = Depends(get_actor_permissions),
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> UserResponse:
    """Update user fields.

    - ``metadata`` is **deep-merged** into existing metadata, not replaced.
    - Set a metadata key to ``null`` to remove it.
    - Send ``name: null`` or ``email: null`` to clear those fields.
    - At least one field must be provided.
    - ``role`` never changes via this endpoint — any ``role`` key is
      rejected with 422 at the schema boundary (``extra="forbid"``).
    - ``permissions`` may only be changed by an admin (403 otherwise —
      the service enforces this via ``is_admin``); you cannot change
      your own permissions.
    - The root user's role cannot be changed via this endpoint (422).

    Uses ``model_dump(exclude_unset=True)`` so that ``None`` means
    "set to null" and an absent key means "do not update."
    """
    update_fields = body.model_dump(exclude_unset=True)
    if not update_fields:
        raise ValidationError(
            "At least one field (name, email, metadata, permissions) "
            "must be provided for update",
        )
    # note: reuses the canonical DB-verified gate so the flag cannot drift
    # from the dependency above; only 403 (non-admin) is absorbed,
    # 503 (no cache) still fails closed.
    try:
        await _ensure_admin(request, _caller_id, db)
        is_admin = True
    except HTTPException as exc:
        if exc.status_code != status.HTTP_403_FORBIDDEN:
            raise
        is_admin = False
    if is_admin:
        # note: an admin edits another member's row — the tenant policy
        # would match zero rows, so this call runs under a bypass on the
        # request session (same session the service was built on).
        async with rls_bypass(db):
            return await service.update_user(
                user_id=user_id,
                update_fields=update_fields,
                actor_user_id=UUID(request.state.user_id),
                actor_permissions=actor_permissions,
                is_admin=is_admin,
            )
    return await service.update_user(

        user_id=user_id,
        update_fields=update_fields,
        actor_user_id=UUID(request.state.user_id),
        actor_permissions=actor_permissions,
        is_admin=is_admin,
    )


@router.delete("/{user_id}", status_code=204)
@audit_action("user.delete", "user", "User deleted")
async def delete_user(
    user_id: UUID,
    request: Request,
    service: UserService = Depends(get_admin_user_service),
    _writer_id: str = Depends(require_admin),  # noqa: B008
) -> Response:
    """Delete a user and all associated data.

    This is a two-phase process:
    1. **Now:** Soft-delete the user (``is_deleted=true``). The user is
       immediately invisible to GET/list queries.
    2. **After 30 days:** A scheduled ARQ worker task performs a
       hard-delete and removes all associated data (episodes, facts,
       sessions, graph nodes).

    If you re-create a user with the same ``external_id`` within the 30-day
    grace period, it will be treated as a new user.

    Admin-gated (JWT platform admin only).  You cannot delete
    your own account, the organization's last admin (422), or the root
    account (422).
    """
    await service.delete_user(

        user_id=user_id,
        actor_user_id=UUID(request.state.user_id),
    )
    return Response(status_code=204)


# ═══════════════════════════════════════════════════════════════════════════════
# User Summary
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/{user_id}/summary", response_model=UserSummaryResponse)
async def get_user_summary(
    user_id: UUID,
    request: Request,
    service: UserSummaryService = Depends(get_user_summary_service),
    _viewer_id: str = Depends(require_admin_or_self()),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> UserSummaryResponse:
    """Get the current summary for a user.

    Readable by the target user themself or a platform admin (JWT only).

    Returns 404 if no summary has been generated yet.
    """
    # note: same DB-verified gate as update_user — an admin reading
    # another member's row runs under a bypass, self reads keep the
    # tenant session. Only 403 absorbed, 503 re-raised.
    try:
        await _ensure_admin(request, _viewer_id, db)
        is_admin = True
    except HTTPException as exc:
        if exc.status_code != status.HTTP_403_FORBIDDEN:
            raise
        is_admin = False
    if is_admin:
        async with rls_bypass(db):
            summary = await service.get_summary(user_id=user_id)
    else:
        summary = await service.get_summary(user_id=user_id)
    if summary is None:
        raise HTTPException(
            status_code=404,
            detail="No summary has been generated for this user yet.",
        )
    return summary


@router.post(
    "/{user_id}/summary",
    status_code=202,
    responses={
        202: {"model": UserSummaryTriggerResponse},
        429: {"description": "Rate limited — try again in 5 minutes."},
    },
)
@audit_action("user.summary.generate", "user", "Summary generated")
async def trigger_user_summary(
    user_id: UUID,
    service: UserSummaryService = Depends(get_user_summary_service),
    _writer_id: str = Depends(require_admin_or_self()),  # noqa: B008
) -> UserSummaryTriggerResponse:
    """Trigger generation of a user summary.

    Enqueues an ARQ background job.  Gated on the target user themself or
    a platform admin (JWT only): this mutates another user's data
    and enqueues paid LLM work.  Rate-limited to once per 5 minutes
    per user (enforced via Redis).
    """
    try:
        return await service.trigger_generation(
            user_id=user_id,
        )
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=exc.message) from exc


@router.get(
    "/{user_id}/summary-instructions",
    response_model=CustomInstructionsResponse,
)
async def list_user_summary_instructions(
    user_id: UUID,
    request: Request,
    service: UserSummaryService = Depends(get_user_summary_service),
    _viewer_id: str = Depends(require_admin_or_self()),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> CustomInstructionsResponse:
    """List custom instructions for a user's summary generation.

    Readable by the target user themself or a platform admin (JWT only).
    """
    # note: same DB-verified gate as update_user — an admin reading
    # another member's rows runs under a bypass, self reads keep the
    # tenant session. Only 403 absorbed, 503 re-raised.
    try:
        await _ensure_admin(request, _viewer_id, db)
        is_admin = True
    except HTTPException as exc:
        if exc.status_code != status.HTTP_403_FORBIDDEN:
            raise
        is_admin = False
    if is_admin:
        async with rls_bypass(db):
            instructions = await service.get_instructions(
                user_id=user_id,
            )
    else:
        instructions = await service.get_instructions(
            user_id=user_id,
        )
    return CustomInstructionsResponse(
        data=[CustomInstructionSchema(**i) for i in instructions],
    )


@router.put(
    "/{user_id}/summary-instructions",
    response_model=CustomInstructionsResponse,
    status_code=201,
)
@audit_action("user.summary.update", "user", "Summary instructions updated")
async def set_user_summary_instructions(
    user_id: UUID,
    body: SetCustomInstructionsRequest,
    request: Request,
    service: UserSummaryService = Depends(get_user_summary_service),
    _writer_id: str = Depends(require_admin_or_self()),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> CustomInstructionsResponse:
    """Replace all summary instructions for a user.

    Gated on the target user themself or a platform admin (JWT
    only): mutates another user's data.
    """
    instructions_data = [i.model_dump() for i in body.instructions]
    # note: same DB-verified gate as update_user — an admin writing
    # another member's rows runs under a bypass, self writes keep the
    # tenant session.
    try:
        await _ensure_admin(request, _writer_id, db)
        is_admin = True
    except HTTPException as exc:
        if exc.status_code != status.HTTP_403_FORBIDDEN:
            raise
        is_admin = False
    if is_admin:
        async with rls_bypass(db):
            instructions = await service.set_instructions(
                user_id=user_id,
                instructions=instructions_data,
            )
    else:
        instructions = await service.set_instructions(
            user_id=user_id,
            instructions=instructions_data,
        )
    return CustomInstructionsResponse(
        data=[CustomInstructionSchema(**i) for i in instructions],
    )


@router.delete("/{user_id}/summary-instructions", status_code=204)
@audit_action("user.summary.delete", "user", "Summary instructions deleted")
async def delete_user_summary_instructions(
    user_id: UUID,
    request: Request,
    service: UserSummaryService = Depends(get_user_summary_service),
    _writer_id: str = Depends(require_admin_or_self()),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Clear all summary instructions for a user.

    Gated on the target user themself or a platform admin (JWT
    only): mutates another user's data.
    """
    # note: same DB-verified gate as update_user — an admin clearing
    # another member's rows runs under a bypass, self writes keep the
    # tenant session.
    try:
        await _ensure_admin(request, _writer_id, db)
        is_admin = True
    except HTTPException as exc:
        if exc.status_code != status.HTTP_403_FORBIDDEN:
            raise
        is_admin = False
    if is_admin:
        async with rls_bypass(db):
            await service.delete_instructions(user_id=user_id)
    else:
        await service.delete_instructions(user_id=user_id)
    return Response(status_code=204)
