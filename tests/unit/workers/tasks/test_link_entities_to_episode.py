"""Unit tests for link_entities_to_episode task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.exceptions import EpisodeNotFoundError

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_CONTENT = "Alice met Bob at the GraphQL conference."
_ROLE = "user"


@pytest.mark.unit
class TestLinkEntitiesToEpisode:
    """link_entities_to_episode task tests."""

    def _mock_db(self) -> AsyncMock:
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        # Return a found episode
        episode_mock = MagicMock()
        episode_mock.id = _EPISODE_ID
        episode_mock.enrichment_status = 0
        default_result = MagicMock()
        default_result.scalar_one_or_none.return_value = episode_mock
        db.execute.return_value = default_result
        return db

    def _mock_ctx(self) -> dict:
        factory = MagicMock()
        factory.return_value = self._mock_db()
        return {
            "redis": AsyncMock(),
            "db_engine": MagicMock(),
            "db_session_factory": factory,
        }

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Entity links created correctly for existing entities."""
        mock_backend = AsyncMock()
        mock_backend.bulk_search_entities.return_value = [
            {"id": str(uuid4()), "name": "Alice", "entity_type": "person"},
            {"id": str(uuid4()), "name": "Bob", "entity_type": "person"},
        ]

        with (
            patch("workers.tasks.link_entities_to_episode.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.link_entities_to_episode.resolve_graph_backend", return_value=mock_backend),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
            patch("services.worker.worker_settings.settings", MagicMock(AUTO_RUN_COMMUNITY_DETECTION=False)),
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo_cls.return_value = mock_repo

            from workers.tasks.link_entities_to_episode import link_entities_to_episode

            await link_entities_to_episode(
                ctx=self._mock_ctx(),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
                role=_ROLE,
            )

            assert mock_backend.bulk_search_entities.call_count >= 1
            assert mock_backend.link_entity_to_episode.call_count >= 1
            mock_repo.apply_enrichment_bits.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_matching_entities(self) -> None:
        """No entities match content → still completes and sets bit."""
        mock_backend = AsyncMock()
        mock_backend.bulk_search_entities.return_value = []

        with (
            patch("workers.tasks.link_entities_to_episode.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.link_entities_to_episode.resolve_graph_backend", return_value=mock_backend),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
            patch("services.worker.worker_settings.settings", MagicMock(AUTO_RUN_COMMUNITY_DETECTION=False)),
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo_cls.return_value = mock_repo

            from workers.tasks.link_entities_to_episode import link_entities_to_episode

            await link_entities_to_episode(
                ctx=self._mock_ctx(),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content="short text",
                role=_ROLE,
            )

            mock_repo.apply_enrichment_bits.assert_called_once()

    @pytest.mark.asyncio
    async def test_episode_not_found(self) -> None:
        """Unknown episode_id raises EpisodeNotFoundError."""
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        default_result = MagicMock()
        default_result.scalar_one_or_none.return_value = None
        db.execute.return_value = default_result
        factory = MagicMock()
        factory.return_value = db

        with patch("workers.tasks.link_entities_to_episode.with_retry", lambda **kw: lambda f: f):
            from workers.tasks.link_entities_to_episode import link_entities_to_episode

            with pytest.raises(EpisodeNotFoundError):
                await link_entities_to_episode(
                    ctx={"db_engine": MagicMock(), "db_session_factory": factory},
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                    role=_ROLE,
                )

    @pytest.mark.asyncio
    async def test_backend_unavailable(self) -> None:
        """Graph backend unavailable → still completes (non-critical)."""
        with (
            patch("workers.tasks.link_entities_to_episode.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.link_entities_to_episode.resolve_graph_backend", return_value=None),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
            patch("services.worker.worker_settings.settings", MagicMock(AUTO_RUN_COMMUNITY_DETECTION=False)),
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo_cls.return_value = mock_repo

            from workers.tasks.link_entities_to_episode import link_entities_to_episode

            await link_entities_to_episode(
                ctx=self._mock_ctx(),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
                role=_ROLE,
            )

            mock_repo.apply_enrichment_bits.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_error_propagates(self) -> None:
        """Database errors are not silently swallowed."""
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        db.execute.side_effect = Exception("DB connection lost")
        factory = MagicMock()
        factory.return_value = db

        with patch("workers.tasks.link_entities_to_episode.with_retry", lambda **kw: lambda f: f):
            from workers.tasks.link_entities_to_episode import link_entities_to_episode

            with pytest.raises(Exception):
                await link_entities_to_episode(
                    ctx={"db_engine": MagicMock(), "db_session_factory": factory},
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                    role=_ROLE,
                )
