"""Admin API Key management endpoints — HTTP adapter layer only.

Endpoints:
    GET    /v1/admin/api-keys       — List API keys for the organization
    POST   /v1/admin/api-keys       — Create a new API key
    DELETE /v1/admin/api-keys/{id}  — Revoke an API key

All endpoints require JWT authentication (dashboard session).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from dependencies.auth import get_dashboard_user, require_org_id
from dependencies.db import get_db
from repositories.api_key_repository import ApiKeyRepository
from schemas.api_keys import (
    ApiKeyCreatedResponse,
    ApiKeyListResponse,
    ApiKeyResponse,
    CreateApiKeyRequest,
)
from utils.crypto import compute_lookup_hash, generate_api_key, hash_api_key

router = APIRouter(
    prefix="/v1/admin/api-keys",
    tags=["Admin - API Keys"],
)


def _get_repo(
    db: AsyncSession = Depends(get_db),
) -> ApiKeyRepository:
    """Dependency factory for ``ApiKeyRepository``."""
    return ApiKeyRepository(db=db)


@router.get(
    "",
    response_model=ApiKeyListResponse,
    summary="List API keys",
    description=(
        "Returns all non-revoked API keys for the authenticated "
        "organization.  Requires a JWT dashboard token."
    ),
)
async def list_api_keys(
    repo: ApiKeyRepository = Depends(_get_repo),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> ApiKeyListResponse:
    """List non-revoked API keys for the organization.

    Args:
        repo: API key repository.
        org_id: Authenticated organization ID.
        _user_id: Authenticated dashboard user ID.

    Returns:
        List of API keys with metadata.
    """
    keys = await repo.list_by_org(
        organization_id=UUID(org_id), include_revoked=False
    )
    return ApiKeyListResponse(
        data=[ApiKeyResponse.model_validate(k) for k in keys],
        total=len(keys),
    )


@router.post(
    "",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create API key",
    description=(
        "Creates a new API key for the organization.  The raw key is "
        "returned exactly once — save it immediately.  Requires a JWT "
        "dashboard token."
    ),
)
async def create_api_key(
    payload: CreateApiKeyRequest,
    repo: ApiKeyRepository = Depends(_get_repo),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> ApiKeyCreatedResponse:
    """Create a new API key.

    Args:
        payload: Key name/label.
        repo: API key repository.
        org_id: Authenticated organization ID.
        _user_id: Authenticated dashboard user ID.

    Returns:
        The new key with the raw value (shown once).
    """
    org_uuid = UUID(org_id)

    raw_key = generate_api_key(prefix="mg_live_")
    key_hash, salt = hash_api_key(raw_key)
    lookup_hash = compute_lookup_hash(raw_key)

    api_key = await repo.create(
        organization_id=org_uuid,
        lookup_hash=lookup_hash,
        key_hash=key_hash,
        salt=salt,
        prefix="mg_live_",
        name=payload.name,
        scopes=["read", "write"],
    )

    return ApiKeyCreatedResponse(
        id=api_key.id,
        name=api_key.name or payload.name,
        prefix=api_key.prefix,
        scopes=list(api_key.scopes),
        is_revoked=api_key.is_revoked,
        last_used_at=api_key.last_used_at,
        created_at=api_key.created_at,
        raw_key=raw_key,
    )


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke API key",
    description=(
        "Revokes an API key by ID.  The key is soft-deleted and can "
        "no longer authenticate requests.  Requires a JWT dashboard token."
    ),
)
async def revoke_api_key(
    key_id: UUID,
    repo: ApiKeyRepository = Depends(_get_repo),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> None:
    """Revoke (soft-delete) an API key.

    Args:
        key_id: UUID of the key to revoke.
        repo: API key repository.
        org_id: Authenticated organization ID.
        _user_id: Authenticated dashboard user ID.

    Raises:
        NotFoundError: If the key does not exist in this organization.
    """
    revoked = await repo.revoke(
        organization_id=UUID(org_id), key_id=key_id
    )
    if revoked is None:
        raise NotFoundError(f"API key '{key_id}' not found.")
