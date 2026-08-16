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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from core.audit import audit_action
from core.exceptions import RateLimitError, ValidationError
from dependencies.auth import (
    require_org_id,
    require_permission,
    require_permission_or_self,
)
from dependencies.db import get_db
from repositories.user_repository import UserRepository
from schemas.custom_instructions import (
    CustomInstructionSchema,
    CustomInstructionsResponse,
    SetCustomInstructionsRequest,
)
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


async def get_user_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserService:
    """FastAPI dependency that yields an initialised :class:`UserService`.

    Wires up the repository and service layers with the request-scoped
    database session and the app-level Redis client (used for role-cache
    invalidation on role changes / deletions).
    """
    repo = UserRepository(db=db)
    redis = getattr(request.app.state, "redis", None)
    return UserService(repo=repo, redis=redis)


@router.post("", response_model=UserResponse, status_code=201)
@audit_action("user.create", "user", "User created")
async def create_user(
    body: CreateUserRequest,
    org_id: str = Depends(require_permission("members:write")),
    service: UserService = Depends(get_user_service),
) -> UserResponse:
    """Create a new user.

    The ``external_id`` is caller-defined and must be unique within the
    organization. Returns 409 if a user with this ``external_id`` already
    exists.  Admin-gated (JWT org admin only).
    """
    return await service.create_user(
        organization_id=UUID(org_id),
        external_id=body.external_id,
        name=body.name,
        email=body.email,
        metadata=body.metadata,
        role=body.role,
    )


@router.get("", response_model=UserListResponse)
async def list_users(
    org_id: str = Depends(require_org_id),
    service: UserService = Depends(get_user_service),
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
) -> UserListResponse:
    """List users with pagination and search.

    Supports cursor-based pagination, multi-field search, and date-range
    filtering. All filters are composable.
    """
    return await service.list_users(
        organization_id=UUID(org_id),
        limit=limit,
        cursor=cursor,
        search=search,
        created_after=created_after,
        created_before=created_before,
    )


@router.get("/{user_id}", response_model=UserResponseWithStats)
async def get_user(
    user_id: UUID,
    service: UserService = Depends(get_user_service),
    org_id: str = Depends(require_org_id),
) -> UserResponseWithStats:
    """Get a user by internal UUID.

    Returns profile information plus aggregate statistics
    (message_count, fact_count, session_count).
    """
    return await service.get_user(organization_id=UUID(org_id), user_id=user_id)


@router.patch("/{user_id}", response_model=UserResponse)
@audit_action("user.update", "user", "User updated")
async def update_user(
    user_id: UUID,
    body: UpdateUserRequest,
    request: Request,
    service: UserService = Depends(get_user_service),
    org_id: str = Depends(require_permission("members:write")),
) -> UserResponse:
    """Update user fields.

    - ``metadata`` is **deep-merged** into existing metadata, not replaced.
    - Set a metadata key to ``null`` to remove it.
    - Send ``name: null`` or ``email: null`` to clear those fields.
    - At least one field must be provided.
    - ``role`` may only be changed by a member with write access (JWT) —
      API keys are rejected upstream by ``require_permission("members:write")``
      (401), and you cannot change your own role.

    Uses ``model_dump(exclude_unset=True)`` so that ``None`` means
    "set to null" and an absent key means "do not update."
    """
    update_fields = body.model_dump(exclude_unset=True)
    if not update_fields:
        raise ValidationError(
            "At least one field (name, email, metadata, role) must be "
            "provided for update",
        )
    return await service.update_user(
        organization_id=UUID(org_id),
        user_id=user_id,
        update_fields=update_fields,
        actor_user_id=UUID(request.state.user_id),
    )


@router.delete("/{user_id}", status_code=204)
@audit_action("user.delete", "user", "User deleted")
async def delete_user(
    user_id: UUID,
    request: Request,
    service: UserService = Depends(get_user_service),
    org_id: str = Depends(require_permission("members:write")),
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

    Admin-gated (JWT org admin only).  You cannot delete your own account
    or the organization's last admin.
    """
    await service.delete_user(
        organization_id=UUID(org_id),
        user_id=user_id,
        actor_user_id=UUID(request.state.user_id),
    )
    return Response(status_code=204)


# ═══════════════════════════════════════════════════════════════════════════════
# User Summary
# ═══════════════════════════════════════════════════════════════════════════════


async def get_user_summary_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserSummaryService:
    """Dependency that yields an initialised UserSummaryService."""
    from core.arq import get_arq

    arq = get_arq()
    redis = getattr(request.app.state, "redis", None)
    return UserSummaryService(db=db, arq=arq, redis=redis)


@router.get("/{user_id}/summary", response_model=UserSummaryResponse)
async def get_user_summary(
    user_id: UUID,
    service: UserSummaryService = Depends(get_user_summary_service),
    org_id: str = Depends(require_permission_or_self("members:read")),
) -> UserSummaryResponse:
    """Get the current summary for a user.

    Readable by the target user themself or an org admin (JWT only).

    Returns 404 if no summary has been generated yet.
    """
    summary = await service.get_summary(org_id=UUID(org_id), user_id=user_id)
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
    org_id: str = Depends(require_permission("members:write")),
) -> UserSummaryTriggerResponse:
    """Trigger generation of a user summary.

    Enqueues an ARQ background job.  Permission-gated
    (``require_permission("members:write")``): this mutates another user's
    data and enqueues paid LLM work.  Rate-limited to once per 5 minutes
    per user (enforced via Redis).
    """
    try:
        return await service.trigger_generation(
            org_id=UUID(org_id), user_id=user_id,
        )
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=exc.message) from exc


@router.get(
    "/{user_id}/summary-instructions",
    response_model=CustomInstructionsResponse,
)
async def list_user_summary_instructions(
    user_id: UUID,
    service: UserSummaryService = Depends(get_user_summary_service),
    org_id: str = Depends(require_permission_or_self("members:read")),
) -> CustomInstructionsResponse:
    """List custom instructions for a user's summary generation.

    Readable by the target user themself or an org admin (JWT only).
    """
    instructions = await service.get_instructions(
        org_id=UUID(org_id), user_id=user_id,
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
    service: UserSummaryService = Depends(get_user_summary_service),
    org_id: str = Depends(require_permission("members:write")),
) -> CustomInstructionsResponse:
    """Replace all summary instructions for a user.

    Permission-gated (``require_permission("members:write")``): mutates
    another user's data.
    """
    instructions_data = [i.model_dump() for i in body.instructions]
    instructions = await service.set_instructions(
        org_id=UUID(org_id),
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
    service: UserSummaryService = Depends(get_user_summary_service),
    org_id: str = Depends(require_permission("members:write")),
) -> Response:
    """Clear all summary instructions for a user.

    Permission-gated (``require_permission("members:write")``): mutates
    another user's data.
    """
    await service.delete_instructions(org_id=UUID(org_id), user_id=user_id)
    return Response(status_code=204)
