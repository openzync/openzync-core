"""Unit tests for embed_episode task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_CONTENT = "Test episode content for embedding."
_TRACE_ID = "trace-101"


@pytest.mark.unit
class TestEmbedEpisode:
    """embed_episode task tests."""

    def _make_db(self) -> AsyncMock:
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        return db

    def _factory(self, db: AsyncMock) -> MagicMock:
        f = MagicMock()
        f.return_value = db
        return f

    def _ctx(self, db: AsyncMock) -> dict:
        return {
            "db_engine": MagicMock(),
            "db_session_factory": self._factory(db),
            "openbao_client": MagicMock(),
        }

    def _make_org_config(self, **overrides) -> MagicMock:
        cfg = MagicMock()
        cfg.embedding_backend = overrides.get("embedding_backend", "openai")
        cfg.embedding_model = overrides.get("embedding_model", "text-embedding-3-small")
        cfg.embedding_dim = overrides.get("embedding_dim", 1536)
        return cfg

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Embedding generated and stored successfully."""
        embedding = [0.1] * 1536

        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            mock_cfg.return_value = self._make_org_config()

            mock_llm = AsyncMock()
            mock_result = MagicMock()
            mock_result.embeddings = [embedding]
            mock_llm.embed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = episode
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.embed_episode import embed_episode

            await embed_episode(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
                trace_id=_TRACE_ID,
            )

            mock_llm.embed.assert_called_once()
            mock_repo.apply_enrichment_bits.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_embedded(self) -> None:
        """Bit 1 already set → skip embedding."""
        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 1 << 1

            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = episode
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.embed_episode import embed_episode

            await embed_episode(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
            )

            mock_repo.apply_enrichment_bits.assert_not_called()

    @pytest.mark.asyncio
    async def test_episode_not_found(self) -> None:
        """Missing episode raises EpisodeNotFoundError."""
        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = None
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.embed_episode import embed_episode

            with pytest.raises(Exception):
                await embed_episode(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_no_embedding_backend(self) -> None:
        """No embedding backend configured → raises."""
        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            cfg = self._make_org_config()
            cfg.embedding_backend = None
            mock_cfg.return_value = cfg

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = episode
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.embed_episode import embed_episode

            with pytest.raises(Exception):
                await embed_episode(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_embedding_failure(self) -> None:
        """Embedding API failure propagates."""
        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            mock_cfg.return_value = self._make_org_config()

            mock_llm = AsyncMock()
            mock_llm.embed.side_effect = Exception("OpenAI API error")
            mock_llm_cls.return_value = mock_llm

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = episode
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.embed_episode import embed_episode

            with pytest.raises(Exception, match="OpenAI API error"):
                await embed_episode(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_dimension_mismatch(self) -> None:
        """Embedding dimension mismatch raises ValueError."""
        embedding = [0.1] * 512  # Wrong dimension

        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            mock_cfg.return_value = self._make_org_config(embedding_dim=1536)

            mock_llm = AsyncMock()
            mock_result = MagicMock()
            mock_result.embeddings = [embedding]
            mock_llm.embed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = episode
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.embed_episode import embed_episode

            with pytest.raises(ValueError, match="dimension mismatch"):
                await embed_episode(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_empty_content(self) -> None:
        """Empty content generates embedding (still valid)."""
        embedding = [0.1] * 1536

        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            mock_cfg.return_value = self._make_org_config()

            mock_llm = AsyncMock()
            mock_result = MagicMock()
            mock_result.embeddings = [embedding]
            mock_llm.embed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = episode
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.embed_episode import embed_episode

            await embed_episode(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content="",
            )

            mock_llm.embed.assert_called_once()
            mock_repo.apply_enrichment_bits.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_content_fetch(self) -> None:
        """When content is None, fetch from DB."""
        embedding = [0.1] * 1536

        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            mock_cfg.return_value = self._make_org_config()

            mock_llm = AsyncMock()
            mock_result = MagicMock()
            mock_result.embeddings = [embedding]
            mock_llm.embed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0
            episode.content = _CONTENT

            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = episode
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.embed_episode import embed_episode

            await embed_episode(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=None,
            )

            mock_llm.embed.assert_called_once()
