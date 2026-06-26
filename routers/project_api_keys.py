"""Project-scoped API Key management endpoints — HTTP adapter layer only.

Endpoints:
    GET    /v1/projects/{project_id}/api-keys      — List API keys for the project
    POST   /v1/projects/{project_id}/api-keys      — Create a new project-scoped API key
    DELETE /v1/projects/{project_id}/api-keys/{id}  — Revoke a project-scoped API key

All endpoints require project owner access (JWT dashboard session).
"""

from __future__ import annotations

from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.redis import get_redis
from dependencies.auth import get_current_user_id, require_org_id
from dependencies.db import get_db
from dependencies.project_auth import require_project_owner
from dependencies.services import get_auth_service
from repositories.api_key_repository import ApiKeyRepository
from schemas.api_keys import (
    ApiKeyCreatedResponse,
    ApiKeyListResponse,
    ApiKeyResponse,
    CreateApiKeyRequest,
)
from services.api_key_service import ApiKeyService
from services.auth_service import AuthService

router = APIRouter(
    prefix="/v1/projects/{project_id}/api-keys",
    tags=["Project - API Keys"],
)


def _get_service(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> ApiKeyService:
    """Dependency factory for ``ApiKeyService``."""
    return ApiKeyService(repo=ApiKeyRepository(db=db), redis=redis)


@router.get(
    "",
    response_model=ApiKeyListResponse,
    summary="List project API keys",
    description=(
        "Returns all non-revoked API keys scoped to the given project. "
        "Requires project owner access (JWT dashboard session)."
    ),
)
async def list_api_keys(
    project_id: UUID,
    service: ApiKeyService = Depends(_get_service),
    _: None = Depends(require_project_owner),
    org_id: str = Depends(require_org_id),
) -> ApiKeyListResponse:
    """List non-revoked API keys for the project.

    Args:
        project_id: Injected from the URL path.
        service: API key service.
        _: Project owner auth guard.
        org_id: Authenticated organization ID.

    Returns:
        List of API keys with metadata.
    """
    keys = await service.list_project_keys(
        organization_id=UUID(org_id),
        project_id=project_id,
    )
    return ApiKeyListResponse(
        data=[ApiKeyResponse.model_validate(k) for k in keys],
        total=len(keys),
    )


@router.post(
    "",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create project API key",
    description=(
        "Creates a new API key scoped to the project.  The raw key is "
        "returned exactly once — save it immediately.  Requires project "
        "owner access (JWT dashboard session)."
    ),
)
async def create_api_key(
    project_id: UUID,
    payload: CreateApiKeyRequest,
    service: ApiKeyService = Depends(_get_service),
    auth_service: AuthService = Depends(get_auth_service),
    _: None = Depends(require_project_owner),
    org_id: str = Depends(require_org_id),
    user_id: UUID = Depends(get_current_user_id),
) -> ApiKeyCreatedResponse:
    """Create a new project-scoped API key.

    Args:
        project_id: Injected from the URL path.
        payload: Key name/label.
        service: API key service.
        auth_service: Auth service for email verification check.
        _: Project owner auth guard.
        org_id: Authenticated organization ID.
        user_id: Authenticated user UUID (stored as ``created_by`` for
            attribution in API-key-authenticated requests).

    Returns:
        The new key with the raw value (shown once).

    Raises:
        HTTPException 403: If the user's email is not verified.
    """
    # ⚠️ SECURITY: require email verification before issuing API keys
    profile = await auth_service.get_profile(user_id)
    if not profile.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email verification required to create API keys. "
            "Please verify your email first.",
        )

    api_key, raw_key = await service.create_project_key(
        organization_id=UUID(org_id),
        project_id=project_id,
        payload=payload,
        created_by=user_id,
    )

    return ApiKeyCreatedResponse(
        id=api_key.id,
        name=api_key.name or payload.name,
        prefix=api_key.prefix,
        project_id=api_key.project_id,
        scopes=list(api_key.scopes),
        is_revoked=api_key.is_revoked,
        last_used_at=api_key.last_used_at,
        created_at=api_key.created_at,
        raw_key=raw_key,
    )


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke project API key",
    description=(
        "Revokes an API key by ID.  The key is soft-deleted and can "
        "no longer authenticate requests.  Requires project owner "
        "access (JWT dashboard session)."
    ),
)
async def revoke_api_key(
    project_id: UUID,
    key_id: UUID,
    service: ApiKeyService = Depends(_get_service),
    _: None = Depends(require_project_owner),
    org_id: str = Depends(require_org_id),
) -> None:
    """Revoke (soft-delete) a project-scoped API key.

    Args:
        project_id: Injected from the URL path.
        key_id: UUID of the key to revoke.
        service: API key service.
        _: Project owner auth guard.
        org_id: Authenticated organization ID.

    Raises:
        NotFoundError: If the key does not exist in this project.
    """
    revoked = await service.revoke_project_key(
        organization_id=UUID(org_id),
        project_id=project_id,
        key_id=key_id,
    )
    if revoked is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API key '{key_id}' not found in this project.",
        )
