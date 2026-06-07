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

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ValidationError
from dependencies.auth import require_org_id
from dependencies.db import get_db
from repositories.user_repository import UserRepository
from schemas.users import (
    CreateUserRequest,
    UpdateUserRequest,
    UserListResponse,
    UserResponse,
    UserResponseWithStats,
)
from services.user_service import UserService

router = APIRouter(prefix="/v1/users", tags=["Users"])


async def get_user_service(
    db: AsyncSession = Depends(get_db),
) -> UserService:
    """FastAPI dependency that yields an initialised :class:`UserService`.

    Wires up the repository and service layers with the request-scoped
    database session.
    """
    repo = UserRepository(db=db)
    return UserService(repo=repo)


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    body: CreateUserRequest,
    org_id: str = Depends(require_org_id),
    service: UserService = Depends(get_user_service),
) -> UserResponse:
    """Create a new user.

    The ``external_id`` is caller-defined and must be unique within the
    organization. Returns 409 if a user with this ``external_id`` already
    exists.
    """
    return await service.create_user(
        organization_id=UUID(org_id),
        external_id=body.external_id,
        name=body.name,
        email=body.email,
        metadata=body.metadata,
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
async def update_user(
    user_id: UUID,
    body: UpdateUserRequest,
    service: UserService = Depends(get_user_service),
    org_id: str = Depends(require_org_id),
) -> UserResponse:
    """Update user fields.

    - ``metadata`` is **deep-merged** into existing metadata, not replaced.
    - Set a metadata key to ``null`` to remove it.
    - Send ``name: null`` or ``email: null`` to clear those fields.
    - At least one field must be provided.

    Uses ``model_dump(exclude_unset=True)`` so that ``None`` means
    "set to null" and an absent key means "do not update."
    """
    update_fields = body.model_dump(exclude_unset=True)
    if not update_fields:
        raise ValidationError(
            "At least one field (name, email, metadata) must be "
            "provided for update",
        )
    return await service.update_user(
        organization_id=UUID(org_id),
        user_id=user_id,
        update_fields=update_fields,
    )


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID,
    service: UserService = Depends(get_user_service),
    org_id: str = Depends(require_org_id),
) -> None:
    """Delete a user and all associated data.

    This is a two-phase process:
    1. **Now:** Soft-delete the user (``is_deleted=true``). The user is
       immediately invisible to GET/list queries.
    2. **After 30 days:** A scheduled ARQ worker task performs a
       hard-delete and removes all associated data (episodes, facts,
       sessions, graph nodes).

    If you re-create a user with the same ``external_id`` within the 30-day
    grace period, it will be treated as a new user.
    """
    await service.delete_user(organization_id=UUID(org_id), user_id=user_id)
