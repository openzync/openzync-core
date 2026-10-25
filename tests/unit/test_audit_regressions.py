"""Unit tests: must-fix regression suite (mocked repos/Redis).

Covers, at unit level with mocked boundaries:
- RBAC: admin-in-A / member-in-B → B request resolves to member
  (org-scoped cache keys; two-org sequence).
- Wipe: cross-org / archived / missing project → 404, zero rows touched;
  confirm mismatch → 422.
- CORS: ``CORS_ORIGINS="*"`` → startup ``ValueError``.
- API-key auth on an archived project → 404 (parity with JWT).
- Post-commit: cache-purge Redis failure after commit → still accepted,
  webhooks still emitted.
- Fact dedup: concurrent double ingest → single winner ``job_id`` replay,
  one batch insert total.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import NotFoundError, ValidationError
from dependencies.project_auth import require_project_membership
from schemas.memory import Message
from services.idempotency_service import (
    ContentHashClaim,
    IdempotencyResult,
    IdempotencyStatus,
)
from services.memory_service import MemoryService

pytestmark = pytest.mark.unit

ORG_A = UUID("00000000-0000-0000-0000-0000000000a1")
ORG_B = UUID("00000000-0000-0000-0000-0000000000b2")
USER_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


class _FakeRedis:
    """Minimal dict-backed async Redis for cache-key isolation tests."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def setex(self, key: str, _ttl: int, value: str) -> bool:
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = sum(1 for k in keys if self.store.pop(k, None) is not None)
        return removed


def _user_with_role(role: str) -> MagicMock:
    user = MagicMock()
    user.is_active = True
    user.is_deleted = False
    user.role = role
    user.permissions = []
    return user


class TestRbacOrgScopedKeys:
    """RBAC: roles are cached per ``(org_id, user_id)``, never per user."""

    @pytest.mark.asyncio
    async def test_two_org_sequence_returns_each_org_role(self) -> None:
        """Admin in A, member in B → B lookup returns member, not A's admin."""
        from core import rbac

        redis = _FakeRedis()
        roles = {ORG_A: "admin", ORG_B: "member"}

        def _repo(_db: object) -> MagicMock:
            repo = MagicMock()

            async def _get_by_uuid(org_id: UUID, _user_id: UUID) -> MagicMock:
                return _user_with_role(roles[org_id])

            repo.get_by_uuid.side_effect = _get_by_uuid
            return repo

        with patch.object(rbac, "UserRepository", side_effect=_repo):
            role_a = await rbac.get_org_role(redis, AsyncMock(), ORG_A, USER_ID)  # type: ignore[arg-type]
            role_b = await rbac.get_org_role(redis, AsyncMock(), ORG_B, USER_ID)  # type: ignore[arg-type]

        assert role_a == "admin"
        assert role_b == "member"
        # Both org-scoped keys exist independently in the cache.
        assert rbac._role_cache_key(ORG_A, USER_ID) != rbac._role_cache_key(ORG_B, USER_ID)
        assert len(redis.store) == 2


def _memory_service(**overrides: object) -> MemoryService:
    """MemoryService with benign mocked repos (mirrors test_memory_service)."""
    mock_db = AsyncMock(spec=AsyncSession)
    mock_redis = AsyncMock()
    mock_redis.scan.return_value = (0, [])
    idem = AsyncMock()
    idem.check_idempotency_key.return_value = IdempotencyResult(
        status=IdempotencyStatus.NEW
    )
    idem.check_content_hash.return_value = None
    idem.compute_content_hash = MagicMock(return_value="abc123")
    kwargs: dict[str, object] = {
        "db": mock_db,
        "redis_client": mock_redis,
        "episode_repo": AsyncMock(),
        "session_repo": AsyncMock(),
        "user_repo": AsyncMock(),
        "fact_repo": AsyncMock(),
        "idempotency_service": idem,
        "dedup_repo": AsyncMock(),
        "project_repo": AsyncMock(),
        "blob_repo": AsyncMock(),
    }
    kwargs.update(overrides)
    return MemoryService(**kwargs)  # type: ignore[arg-type]


class TestWipeOrgGate:
    """Wipe: cross-org / archived / missing → 404, zero rows touched."""

    @pytest.mark.asyncio
    async def test_missing_project_404_zero_rows(self) -> None:
        """``get_by_id → None`` → ``NotFoundError`` 404, no deletes."""
        service = _memory_service()
        service._project_repo.get_by_id.return_value = None
        service._blob_repo.get_by_project.return_value = []

        with pytest.raises(NotFoundError) as exc_info:
            await service.delete_project_memory(
                org_id=ORG_A,
                project_id=PROJECT_ID,
                confirm=str(PROJECT_ID),
                actor_id=USER_ID,
            )

        assert exc_info.value.status_code == 404
        service._episode_repo.soft_delete_by_project.assert_not_awaited()
        service._fact_repo.soft_delete_by_project.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirm_mismatch_422_no_lookup(self) -> None:
        """Mismatched ``confirm`` → ``ValidationError`` 422 before any read."""
        service = _memory_service()

        with pytest.raises(ValidationError) as exc_info:
            await service.delete_project_memory(
                org_id=ORG_A,
                project_id=PROJECT_ID,
                confirm=str(uuid4()),
                actor_id=USER_ID,
            )

        assert exc_info.value.status_code == 422
        service._project_repo.get_by_id.assert_not_awaited()
        service._episode_repo.soft_delete_by_project.assert_not_awaited()


class TestCorsWildcardRejected:
    """CORS: ``"*"`` is a startup error while credentials are allowed."""

    def test_wildcard_cors_origin_raises(self) -> None:
        """``CORS_ORIGINS="*"`` → ``ValueError`` at ``Settings`` build."""
        with pytest.raises(PydanticValidationError, match=r"\*"):
            Settings(
                DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/t",
                REDIS_URL="redis://localhost:6379/1",
                SECRET_KEY="a" * 32,
                WEBHOOK_SIGNING_SECRET="b" * 32,
                CORS_ORIGINS="*",
            )


class TestApiKeyArchivedProject:
    """API-key auth on an archived project → 404 (parity with JWT)."""

    @pytest.mark.asyncio
    async def test_api_key_archived_project_404(self) -> None:
        """Org-scoped lookup misses archived rows → 404, same as JWT."""
        request = MagicMock()
        request.state.org_id = str(ORG_A)
        request.state.auth_type = "api_key"
        request.state.api_key_project_id = str(PROJECT_ID)

        with patch(
            "dependencies.project_auth.ProjectRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=None)
            with pytest.raises(HTTPException) as exc_info:
                await require_project_membership(
                    request, project_id=PROJECT_ID, db=AsyncMock()
                )

        assert exc_info.value.status_code == 404


def _sample_messages(count: int = 2) -> list[Message]:
    return [
        Message(role="user" if i % 2 == 0 else "assistant", content=f"Message {i}")
        for i in range(count)
    ]


def _happy_ingest_mocks(service: MemoryService) -> None:
    """Prime a MemoryService mock set for the ingest happy path."""
    service._session_repo.get_by_external_id.return_value = MagicMock(
        id=uuid4(), external_id="session-abc"
    )
    service._idem.claim_content_hash.return_value = ContentHashClaim(
        won=True, winner="pending"
    )
    service._dedup_repo.insert_or_none.return_value = True
    service._episode_repo.batch_create.return_value = []


class TestPostCommitPurgeFailure:
    """Post-commit purge failure → still accepted, webhooks still emitted."""

    @pytest.mark.asyncio
    async def test_purge_failure_still_accepted_and_emits(self) -> None:
        """Redis purge raising after commit → accepted response + 2 emits."""
        webhook_service = AsyncMock()
        service = _memory_service(webhook_service=webhook_service)
        _happy_ingest_mocks(service)

        with (
            patch.object(service, "_enqueue_arq_tasks"),
            patch.object(
                service,
                "_invalidate_context_cache",
                side_effect=RuntimeError("redis down"),
            ),
            patch.object(service, "_get_org_pii_config", return_value={}),
        ):
            result = await service.ingest(
                org_id=ORG_A,
                project_id=PROJECT_ID,
                created_by=USER_ID,
                session_external_id="session-abc",
                messages=_sample_messages(),
            )

        assert result.status == "accepted"
        assert webhook_service.emit.await_count == 2


class TestFactDedupDoubleIngest:
    """Concurrent double ingest → single winner ``job_id`` replay."""

    @pytest.mark.asyncio
    async def test_second_ingest_replays_winner_job_id(self) -> None:
        """Loser replays the winner's ``job_id``; one batch insert total."""
        service = _memory_service()
        _happy_ingest_mocks(service)

        async def _ingest_once() -> str:
            with (
                patch.object(service, "_enqueue_arq_tasks"),
                patch.object(service, "_invalidate_context_cache"),
                patch.object(service, "_get_org_pii_config", return_value={}),
            ):
                result = await service.ingest(
                    org_id=ORG_A,
                    project_id=PROJECT_ID,
                    created_by=USER_ID,
                    session_external_id="session-abc",
                    messages=_sample_messages(),
                )
            assert result.job_id is not None
            return result.job_id

        winner_job_id = await _ingest_once()

        # Second identical ingest loses the atomic claim → replays winner.
        service._idem.claim_content_hash.return_value = ContentHashClaim(
            won=False, winner=f"{winner_job_id}:2"
        )
        with (
            patch.object(service, "_enqueue_arq_tasks"),
            patch.object(service, "_invalidate_context_cache"),
            patch.object(service, "_get_org_pii_config", return_value={}),
        ):
            replay = await service.ingest(
                org_id=ORG_A,
                project_id=PROJECT_ID,
                created_by=USER_ID,
                session_external_id="session-abc",
                messages=_sample_messages(),
            )

        assert replay.job_id == winner_job_id
        assert service._episode_repo.batch_create.await_count == 1
