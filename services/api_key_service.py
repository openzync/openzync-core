"""API Key service — business logic for project-scoped API key management.

Handles key generation, hashing, and persistence.  Sits between
``routers/project_api_keys.py`` and ``repositories/api_key_repository.py``
to keep business logic out of the HTTP layer.

Every key created through this service is scoped to a project — there are
no org-wide API keys.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from models.api_key import ApiKey
from repositories.api_key_repository import ApiKeyRepository
from schemas.api_keys import CreateApiKeyRequest
from utils.crypto import compute_lookup_hash, generate_api_key, hash_api_key

logger = structlog.get_logger(__name__)


class ApiKeyService:
    """Business logic for API key lifecycle management.

    Args:
        repo: The API key repository instance.
    """

    def __init__(self, repo: ApiKeyRepository) -> None:
        self._repo = repo

    async def create_project_key(
        self,
        organization_id: UUID,
        project_id: UUID,
        payload: CreateApiKeyRequest,
    ) -> tuple[ApiKey, str]:
        """Create a new API key scoped to a specific project.

        Generates a cryptographically random key, hashes it, and persists
        the hash.  The raw key is returned exactly once.

        Args:
            organization_id: The owning organization UUID.
            project_id: The project UUID to scope this key to.
            payload: Key name from the request body.

        Returns:
            A tuple of ``(ApiKey record, raw_key_string)``.

        Raises:
            No database-level exceptions are expected (FK constraints
            guarantee org and project exist, checked by the auth dependency).
        """
        raw_key = generate_api_key(prefix="mg_live_")
        key_hash, salt = hash_api_key(raw_key)
        lookup_hash = compute_lookup_hash(raw_key)

        api_key = await self._repo.create(
            organization_id=organization_id,
            project_id=project_id,
            lookup_hash=lookup_hash,
            key_hash=key_hash,
            salt=salt,
            prefix="mg_live_",
            name=payload.name,
            scopes=["read", "write"],
        )

        logger.info(
            "api_key.created",
            key_id=str(api_key.id),
            project_id=str(project_id),
            org_id=str(organization_id),
        )

        return api_key, raw_key

    async def list_project_keys(
        self,
        organization_id: UUID,
        project_id: UUID,
    ) -> list[ApiKey]:
        """List all non-revoked API keys for a project.

        Args:
            organization_id: The owning organization UUID.
            project_id: The project UUID to list keys for.

        Returns:
            A list of ``ApiKey`` records, newest first.
        """
        return list(
            await self._repo.list_by_org(
                organization_id=organization_id,
                project_id=project_id,
                include_revoked=False,
            )
        )

    async def revoke_project_key(
        self,
        organization_id: UUID,
        project_id: UUID,
        key_id: UUID,
    ) -> ApiKey | None:
        """Revoke (soft-delete) an API key scoped to a project.

        Args:
            organization_id: The owning organization UUID.
            project_id: The project UUID scope.
            key_id: The UUID of the API key to revoke.

        Returns:
            The revoked ``ApiKey``, or ``None`` if not found within the
            given org + project scope.
        """
        api_key = await self._repo.revoke(
            organization_id=organization_id,
            project_id=project_id,
            key_id=key_id,
        )

        if api_key is not None:
            logger.info(
                "api_key.revoked",
                key_id=str(key_id),
                project_id=str(project_id),
                org_id=str(organization_id),
            )

        return api_key
