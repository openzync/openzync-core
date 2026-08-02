"""Unit tests for user_summary_service — summary generation, retrieval, instructions."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from core.exceptions import CacheUnavailableError, RateLimitError
from services.user_summary_service import UserSummaryService

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


class TestUserSummaryService:
    """Tests for UserSummaryService."""

    def _make_service(
        self,
        has_redis: bool = True,
    ) -> tuple[UserSummaryService, AsyncMock, AsyncMock, AsyncMock | None]:
        """Create service with all dependencies mocked.

        Returns:
            Tuple of (service, mock_arq, mock_user_repo, mock_ci_repo).
            ``mock_redis`` is only available when ``has_redis=True``.
        """
        mock_db = AsyncMock()
        mock_arq = AsyncMock()
        mock_arq.enqueue = AsyncMock(return_value="job-abc")
        mock_redis = AsyncMock() if has_redis else None

        service = UserSummaryService(db=mock_db, arq=mock_arq, redis=mock_redis)
        # Swap out repos for direct mocks — easier than wiring mock db.
        service._user_repo = AsyncMock()
        service._ci_repo = AsyncMock()

        return service, mock_arq, service._user_repo, service._ci_repo

    # ── trigger_generation ────────────────────────────────────────────────

    async def test_trigger_generation_enqueues_job(self) -> None:
        """trigger_generation calls _check_rate_limit, enqueues, returns response."""
        service, mock_arq, mock_user_repo, mock_ci_repo = self._make_service()

        # Rate limit allows
        service._redis.set.return_value = True  # nx=True -> key was set

        response = await service.trigger_generation(org_id=ORG_ID, user_id=USER_ID)

        assert response.user_id == USER_ID
        assert response.status == "processing"
        assert "started" in response.message

        service._redis.set.assert_awaited_once_with(
            f"ratelimit:summary:{ORG_ID}:{USER_ID}",
            "1",
            nx=True,
            ex=300,
        )
        mock_arq.enqueue.assert_awaited_once()

    async def test_trigger_generation_rate_limited(self) -> None:
        """Second trigger within 5 minutes raises RateLimitError."""
        service, mock_arq, mock_user_repo, mock_ci_repo = self._make_service()

        # Rate limit blocks — SET NX returns None (key already exists)
        service._redis.set.return_value = None

        with pytest.raises(RateLimitError, match="rate limited"):
            await service.trigger_generation(org_id=ORG_ID, user_id=USER_ID)

        mock_arq.enqueue.assert_not_awaited()

    async def test_trigger_generation_no_redis_raises_cache_error(self) -> None:
        """Without Redis, trigger_generation raises CacheUnavailableError."""
        service, mock_arq, mock_user_repo, mock_ci_repo = self._make_service(
            has_redis=False,
        )

        with pytest.raises(CacheUnavailableError, match="Redis is required"):
            await service.trigger_generation(org_id=ORG_ID, user_id=USER_ID)

        mock_arq.enqueue.assert_not_awaited()

    # ── get_summary ────────────────────────────────────────────────────────

    async def test_get_summary_returns_summary(self) -> None:
        """Existing summary returns a UserSummaryResponse."""
        service, mock_arq, mock_user_repo, mock_ci_repo = self._make_service()

        now = datetime.now(timezone.utc)
        mock_user_repo.get_summary.return_value = (
            "User is an active contributor.",
            now,
        )

        response = await service.get_summary(org_id=ORG_ID, user_id=USER_ID)

        assert response is not None
        assert response.user_id == USER_ID
        assert response.summary == "User is an active contributor."
        assert response.updated_at == now
        mock_user_repo.get_summary.assert_awaited_once_with(USER_ID)

    async def test_get_summary_no_summary_returns_none(self) -> None:
        """No stored summary returns None."""
        service, mock_arq, mock_user_repo, mock_ci_repo = self._make_service()

        mock_user_repo.get_summary.return_value = (None, None)

        response = await service.get_summary(org_id=ORG_ID, user_id=USER_ID)

        assert response is None
        mock_user_repo.get_summary.assert_awaited_once_with(USER_ID)

    # ── get_instructions ───────────────────────────────────────────────────

    async def test_get_instructions_returns_formatted_list(self) -> None:
        """Custom instructions are returned as {name, text} dicts."""
        service, mock_arq, mock_user_repo, mock_ci_repo = self._make_service()

        mock_item = MagicMock()
        mock_item.name = "tone"
        mock_item.text = "Be concise."
        mock_ci_repo.get_by_scope.return_value = [mock_item]

        result = await service.get_instructions(org_id=ORG_ID, user_id=USER_ID)

        assert result == [{"name": "tone", "text": "Be concise."}]
        mock_ci_repo.get_by_scope.assert_awaited_once_with(
            org_id=ORG_ID,
            scope="user_summary",
            target_id=USER_ID,
        )

    async def test_get_instructions_empty(self) -> None:
        """Empty instructions from repo return empty list."""
        service, mock_arq, mock_user_repo, mock_ci_repo = self._make_service()

        mock_ci_repo.get_by_scope.return_value = []

        result = await service.get_instructions(org_id=ORG_ID, user_id=USER_ID)

        assert result == []

    # ── set_instructions ───────────────────────────────────────────────────

    async def test_set_instructions_replaces_and_returns(self) -> None:
        """set_instructions delegates to repo and formats response."""
        service, mock_arq, mock_user_repo, mock_ci_repo = self._make_service()

        mock_item = MagicMock()
        mock_item.name = "tone"
        mock_item.text = "Formal tone."
        mock_ci_repo.set_by_scope.return_value = [mock_item]

        result = await service.set_instructions(
            org_id=ORG_ID,
            user_id=USER_ID,
            instructions=[{"name": "tone", "text": "Formal tone."}],
        )

        assert result == [{"name": "tone", "text": "Formal tone."}]
        mock_ci_repo.set_by_scope.assert_awaited_once_with(
            org_id=ORG_ID,
            scope="user_summary",
            target_id=USER_ID,
            instructions=[{"name": "tone", "text": "Formal tone."}],
        )

    # ── delete_instructions ────────────────────────────────────────────────

    async def test_delete_instructions_delegates_to_repo(self) -> None:
        """delete_instructions calls the repo method."""
        service, mock_arq, mock_user_repo, mock_ci_repo = self._make_service()

        await service.delete_instructions(org_id=ORG_ID, user_id=USER_ID)

        mock_ci_repo.delete_by_scope.assert_awaited_once_with(
            org_id=ORG_ID,
            scope="user_summary",
            target_id=USER_ID,
        )
