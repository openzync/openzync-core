"""Unit tests for embed_episode task."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_CONTENT = "Test episode content for embedding."
_TRACE_ID = "trace-101"


@pytest.fixture
def embedder():
    """Patch the single embedder entrypoint; auto-undone after the test.

    Yields:
        The ``AsyncMock`` standing in for ``embed_passage``.
    """
    mock = AsyncMock(return_value=[[0.1] * 768])
    with patch("core.embeddings.embed_passage", mock):
        yield mock


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
        }

    @pytest.mark.asyncio
    async def test_success(self, embedder: AsyncMock) -> None:
        """Embedding generated and stored successfully."""
        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
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

            embedder.assert_awaited_once()
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
    async def test_embedder_never_queried_for_org_config(
        self, embedder: AsyncMock
    ) -> None:
        """Embedding needs no org config — the ARQ ctx is not consulted for it.

        Guards the regression that re-introduced a per-org embedding
        backend: a stubbed org config would be silently ignored if some
        caller reached for it again.
        """
        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("core.org_config.get_org_config") as mock_get_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
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
            )

            mock_get_cfg.assert_not_called()

    @pytest.mark.asyncio
    async def test_embedding_failure(self, embedder: AsyncMock) -> None:
        """Embedding API failure propagates."""
        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            embedder.side_effect = Exception("ONNX inference error")

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = episode
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.embed_episode import embed_episode

            with pytest.raises(Exception, match="ONNX inference error"):
                await embed_episode(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_empty_content(self, embedder: AsyncMock) -> None:
        """Empty content generates embedding (still valid)."""
        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
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

            embedder.assert_awaited_once()
            mock_repo.apply_enrichment_bits.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_content_fetch(self, embedder: AsyncMock) -> None:
        """When content is None, fetch from DB."""
        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
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

            embedder.assert_awaited_once()

    # ── RLS tenant context ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_tenant_is_bound_before_the_episode_is_read(
        self, embedder: AsyncMock
    ) -> None:
        """The ``app.org_id`` binding happens before the row is read.

        Under enforced row-level security a session that queries first and
        binds second sees zero rows — the task would report success
        having done nothing at all.

        The binding is transaction-local and carries all three GUCs, so it
        cannot outlive the transaction nor stand on the pooled connection.
        """
        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = episode
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            recorder = Mock()
            recorder.attach_mock(db.execute, "execute")
            recorder.attach_mock(mock_repo.get_by_id, "get_by_id")

            from workers.tasks.embed_episode import embed_episode

            await embed_episode(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
            )

            calls = recorder.mock_calls
            read_index = next(i for i, c in enumerate(calls) if c[0] == "get_by_id")
            # Exactly one statement ran before the read — the tenant bind.
            assert [c[0] for c in calls[:read_index]] == ["execute"]
            assert "app.org_id" in str(calls[0].args[0])
            assert calls[0].args[1] == {
                "org_id": _ORG_ID,
                "bypass_rls": "false",
                "lookup_code": "",
            }

    @pytest.mark.asyncio
    async def test_tenant_context_is_released_before_the_session_closes(
        self, embedder: AsyncMock
    ) -> None:
        """The release rolls back — it issues no reset statement.

        The context is bound transaction-locally, so the end of the
        transaction already discarded it: there is nothing left standing
        on the pooled connection to reset.  The release therefore drops
        the remembered context and rolls back, and nothing else.
        """
        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = episode
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            recorder = Mock()
            recorder.attach_mock(db.execute, "execute")
            recorder.attach_mock(db.rollback, "rollback")
            recorder.attach_mock(db.commit, "commit")

            from workers.tasks.embed_episode import embed_episode

            await embed_episode(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
            )

            ops = [c[0] for c in recorder.mock_calls]
            # The idempotency session: bind → read → release (rollback).
            # The write session: bind → update → commit → release (rollback).
            assert ops == [
                "execute",
                "rollback",
                "execute",
                "execute",
                "commit",
                "rollback",
            ]
            # The release issued no statement of its own — the last
            # statement is the write, not a context reset.
            assert "set_config('app.org_id', ''" not in str(
                db.execute.await_args_list[-1].args[0]
            )
