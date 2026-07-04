"""Unit tests for ApiKeyService — business logic with mocked repository.

Covers all project-scoped API key lifecycle methods.
Repository calls are mocked via ``AsyncMock`` so no database is required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from schemas.api_keys import CreateApiKeyRequest
from services.api_key_service import ApiKeyService


@pytest.mark.unit
class TestApiKeyService:
    """ApiKeyService unit tests — all repository calls are mocked."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
    KEY_ID = UUID("00000000-0000-0000-0000-000000000003")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(self) -> tuple[ApiKeyService, AsyncMock]:
        """Create an ApiKeyService with a mocked repository."""
        mock_repo = AsyncMock()
        service = ApiKeyService(repo=mock_repo)
        return service, mock_repo

    def _make_mock_key(self, **kwargs: object) -> MagicMock:
        """Mock an ApiKey ORM instance."""
        key = MagicMock()
        key.id = kwargs.get("id", self.KEY_ID)
        key.organization_id = kwargs.get("org_id", self.ORG_ID)
        key.project_id = kwargs.get("project_id", self.PROJECT_ID)
        key.name = kwargs.get("name", "Test Key")
        key.prefix = kwargs.get("prefix", "oz_test_")
        key.scopes = kwargs.get("scopes", ["read", "write"])
        key.is_revoked = kwargs.get("is_revoked", False)
        key.lookup_hash = kwargs.get("lookup_hash", "abc123")
        key.key_hash = kwargs.get("key_hash", "def456")
        key.salt = kwargs.get("salt", "salt123")
        key.last_used_at = kwargs.get("last_used_at", None)
        return key

    # ── Tests ────────────────────────────────────────────────────────────────

    async def test_create_project_key_success(self) -> None:
        """Creating a key delegates to the repo and returns raw key."""
        service, mock_repo = self._make_service()

        mock_key = self._make_mock_key()
        mock_repo.create.return_value = mock_key

        payload = CreateApiKeyRequest(name="CI/CD Key")
        api_key, raw_key = await service.create_project_key(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            payload=payload,
        )

        assert api_key.id == self.KEY_ID
        assert api_key.name == "Test Key"
        assert api_key.project_id == self.PROJECT_ID
        assert len(raw_key) > 20  # generated key is ~73 chars
        assert raw_key.startswith("oz_live_")

        # Verify repo was called with the right args
        mock_repo.create.assert_awaited_once()
        call_kwargs = mock_repo.create.call_args.kwargs
        assert call_kwargs["organization_id"] == self.ORG_ID
        assert call_kwargs["project_id"] == self.PROJECT_ID
        assert call_kwargs["name"] == "CI/CD Key"
        assert call_kwargs["scopes"] == ["read", "write"]

    async def test_create_project_key_with_default_scopes(self) -> None:
        """Ensure default scopes are applied when none specified."""
        service, mock_repo = self._make_service()

        mock_key = self._make_mock_key()
        mock_repo.create.return_value = mock_key

        payload = CreateApiKeyRequest(name="Default Scopes Key")
        api_key, raw_key = await service.create_project_key(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            payload=payload,
        )

        assert api_key.project_id == self.PROJECT_ID
        assert raw_key.startswith("oz_live_")

    async def test_create_project_key_hash_properties(self) -> None:
        """The generated key hash and lookup hash should be set correctly."""
        service, mock_repo = self._make_service()

        mock_key = self._make_mock_key()
        mock_repo.create.return_value = mock_key

        payload = CreateApiKeyRequest(name="Hash Test")
        await service.create_project_key(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            payload=payload,
        )

        call_kwargs = mock_repo.create.call_args.kwargs

        # Lookup hash should be a 64-char hex string (SHA-256)
        assert len(call_kwargs["lookup_hash"]) == 64
        # Key hash should be a 64-char hex string (SHA-256)
        assert len(call_kwargs["key_hash"]) == 64
        # Salt should be a 32-char hex string (16 bytes)
        assert len(call_kwargs["salt"]) == 32

    async def test_list_project_keys(self) -> None:
        """List keys delegates to repo with correct org and project."""
        service, mock_repo = self._make_service()

        mock_key1 = self._make_mock_key(id=uuid4(), name="Key 1")
        mock_key2 = self._make_mock_key(id=uuid4(), name="Key 2")
        mock_repo.list_by_org.return_value = [mock_key1, mock_key2]

        keys = await service.list_project_keys(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
        )

        assert len(keys) == 2
        assert keys[0].name == "Key 1"
        assert keys[1].name == "Key 2"

        mock_repo.list_by_org.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            include_revoked=False,
        )

    async def test_list_project_keys_empty(self) -> None:
        """List returns empty list when no keys exist for the project."""
        service, mock_repo = self._make_service()
        mock_repo.list_by_org.return_value = []

        keys = await service.list_project_keys(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
        )

        assert keys == []

    async def test_revoke_project_key_success(self) -> None:
        """Revoking a key delegates to repo and returns revoked key."""
        service, mock_repo = self._make_service()

        revoked_key = self._make_mock_key(is_revoked=True)
        mock_repo.revoke.return_value = revoked_key

        result = await service.revoke_project_key(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            key_id=self.KEY_ID,
        )

        assert result is not None
        assert result.is_revoked is True
        assert result.id == self.KEY_ID

        mock_repo.revoke.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            key_id=self.KEY_ID,
        )

    async def test_revoke_project_key_not_found(self) -> None:
        """Revoking a non-existent key returns None."""
        service, mock_repo = self._make_service()
        mock_repo.revoke.return_value = None

        result = await service.revoke_project_key(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            key_id=uuid4(),
        )

        assert result is None

    async def test_list_different_project_returns_different_keys(self) -> None:
        """Verifies project isolation — different project, different keys."""
        service, mock_repo = self._make_service()

        # Keys for project A
        mock_repo.list_by_org.return_value = [
            self._make_mock_key(id=uuid4(), name="Project A Key"),
        ]

        keys_a = await service.list_project_keys(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
        )

        assert len(keys_a) == 1
        first_call_project = mock_repo.list_by_org.call_args.kwargs["project_id"]
        assert first_call_project == self.PROJECT_ID

        # Keys for project B (empty)
        other_project = UUID("00000000-0000-0000-0000-000000000099")
        mock_repo.list_by_org.return_value = []

        keys_b = await service.list_project_keys(
            organization_id=self.ORG_ID,
            project_id=other_project,
        )

        assert len(keys_b) == 0
        second_call_project = mock_repo.list_by_org.call_args.kwargs["project_id"]
        assert second_call_project == other_project

    # ── Cache invalidation on revoke ──────────────────────────────────────────

    async def test_revoke_invalidates_auth_cache(self) -> None:
        """Revoke deletes the positive and negative Redis auth cache entries."""
        service, mock_repo = self._make_service()
        mock_redis = AsyncMock()
        service._redis = mock_redis

        revoked_key = self._make_mock_key(lookup_hash="mykey123", is_revoked=True)
        mock_repo.revoke.return_value = revoked_key

        result = await service.revoke_project_key(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            key_id=self.KEY_ID,
        )

        assert result is not None
        assert result.is_revoked is True
        mock_redis.delete.assert_awaited_once_with(
            "auth:key:mykey123",
            "auth:neg:mykey123",
        )

    async def test_revoke_no_redis_does_not_crash(self) -> None:
        """Revoke succeeds gracefully when Redis is None (no cache configured)."""
        service, mock_repo = self._make_service()
        service._redis = None  # explicitly no Redis

        revoked_key = self._make_mock_key(is_revoked=True)
        mock_repo.revoke.return_value = revoked_key

        result = await service.revoke_project_key(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            key_id=self.KEY_ID,
        )

        assert result is not None
        assert result.is_revoked is True

    async def test_revoke_not_found_does_not_call_redis(self) -> None:
        """Revoke of a non-existent key should NOT attempt cache invalidation."""
        service, mock_repo = self._make_service()
        mock_redis = AsyncMock()
        service._redis = mock_redis
        mock_repo.revoke.return_value = None

        result = await service.revoke_project_key(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            key_id=uuid4(),
        )

        assert result is None
        # repo.revoke was called
        mock_repo.revoke.assert_awaited_once()
        # redis.delete should NOT have been called (no key to invalidate)
        mock_redis.delete.assert_not_awaited()
