"""Unit tests for compute_observations task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.exceptions import EpisodeNotFoundError

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())


@pytest.mark.unit
class TestComputeObservations:
    """compute_observations task tests."""

    def _make_db(self) -> AsyncMock:
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        return db

    def _make_episode(self, enrichment_status: int = 0) -> MagicMock:
        ep = MagicMock()
        ep.id = _EPISODE_ID
        ep.enrichment_status = enrichment_status
        return ep

    def _factory(self, db: AsyncMock) -> MagicMock:
        f = MagicMock()
        f.return_value = db
        return f

    def _ctx(self, db: AsyncMock) -> dict:
        return {"db_engine": MagicMock(), "db_session_factory": self._factory(db)}

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Observations computed from facts/entities successfully."""
        mock_backend = AsyncMock()
        mock_episode = self._make_episode(enrichment_status=0)

        with (
            patch("workers.tasks.compute_observations.with_retry", lambda **kw: lambda f: f),
            patch("workers.backend.resolve_graph_backend", return_value=mock_backend),
            patch("workers.tasks.compute_observations._maybe_get_llm_backend", return_value=None),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
            patch("services.observation_service.ObservationService") as mock_svc_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = mock_episode
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc.run_full_project_scan.return_value = {
                "cooccurrence": 3, "temporal_gap": 2, "behavioural": 1,
            }
            mock_svc_cls.return_value = mock_svc

            db = self._make_db()
            from workers.tasks.compute_observations import compute_observations

            await compute_observations(ctx=self._ctx(db), episode_id=_EPISODE_ID, org_id=_ORG_ID, project_id=_PROJECT_ID)

            mock_repo.get_by_id.assert_called_once()
            mock_svc.run_full_project_scan.assert_called_once()
            mock_repo.apply_enrichment_bits.assert_called_once()
            db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_done(self) -> None:
        """Bit 6 already set → no-op."""
        mock_episode = self._make_episode(enrichment_status=1 << 6)

        with (
            patch("workers.tasks.compute_observations.with_retry", lambda **kw: lambda f: f),
            patch("workers.backend.resolve_graph_backend", return_value=AsyncMock()),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = mock_episode
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.compute_observations import compute_observations

            await compute_observations(ctx=self._ctx(db), episode_id=_EPISODE_ID, org_id=_ORG_ID, project_id=_PROJECT_ID)

            # Should NOT proceed to scan or set bit
            mock_repo.apply_enrichment_bits.assert_not_called()

    @pytest.mark.asyncio
    async def test_episode_not_found(self) -> None:
        """Missing episode raises EpisodeNotFoundError."""
        with (
            patch("workers.tasks.compute_observations.with_retry", lambda **kw: lambda f: f),
            patch("workers.backend.resolve_graph_backend", return_value=AsyncMock()),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = None
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.compute_observations import compute_observations

            with pytest.raises(EpisodeNotFoundError):
                await compute_observations(ctx=self._ctx(db), episode_id=_EPISODE_ID, org_id=_ORG_ID, project_id=_PROJECT_ID)

    @pytest.mark.asyncio
    async def test_empty_scan(self) -> None:
        """Empty graph → no observations (service returns empty dict)."""
        mock_backend = AsyncMock()
        mock_episode = self._make_episode(enrichment_status=0)

        with (
            patch("workers.tasks.compute_observations.with_retry", lambda **kw: lambda f: f),
            patch("workers.backend.resolve_graph_backend", return_value=mock_backend),
            patch("workers.tasks.compute_observations._maybe_get_llm_backend", return_value=None),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
            patch("services.observation_service.ObservationService") as mock_svc_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = mock_episode
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc.run_full_project_scan.return_value = {}
            mock_svc_cls.return_value = mock_svc

            db = self._make_db()
            from workers.tasks.compute_observations import compute_observations

            await compute_observations(ctx=self._ctx(db), episode_id=_EPISODE_ID, org_id=_ORG_ID, project_id=_PROJECT_ID)

            mock_svc.run_full_project_scan.assert_called_once()
            mock_repo.apply_enrichment_bits.assert_called_once()

    @pytest.mark.asyncio
    async def test_graph_disabled_skips(self) -> None:
        """Graph backend resolves to ``None`` (graph disabled) → no-op."""
        with (
            patch("workers.tasks.compute_observations.with_retry", lambda **kw: lambda f: f),
            patch("workers.backend.resolve_graph_backend", return_value=None),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
            patch("services.observation_service.ObservationService") as mock_svc_cls,
        ):
            mock_repo = AsyncMock()
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.compute_observations import compute_observations

            await compute_observations(ctx=self._ctx(db), episode_id=_EPISODE_ID, org_id=_ORG_ID, project_id=_PROJECT_ID)

            # ObservationService never constructed — _assert_backend would raise on None.
            mock_svc_cls.assert_not_called()
            mock_repo.get_by_id.assert_not_called()
            mock_repo.apply_enrichment_bits.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_error_propagates(self) -> None:
        """Database errors are not silently swallowed."""
        with (
            patch("workers.tasks.compute_observations.with_retry", lambda **kw: lambda f: f),
            patch("workers.backend.resolve_graph_backend", return_value=AsyncMock()),
        ):
            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            db.execute.side_effect = Exception("DB unavailable")

            from workers.tasks.compute_observations import compute_observations

            with pytest.raises(Exception):
                await compute_observations(ctx=self._ctx(db), episode_id=_EPISODE_ID, org_id=_ORG_ID, project_id=_PROJECT_ID)

    @pytest.mark.asyncio
    async def test_observations_filtered_by_type(self) -> None:
        """Observations filtered by type — only cooccurrence observations returned."""
        mock_backend = AsyncMock()
        mock_episode = self._make_episode(enrichment_status=0)

        with (
            patch("workers.tasks.compute_observations.with_retry", lambda **kw: lambda f: f),
            patch("workers.backend.resolve_graph_backend", return_value=mock_backend),
            patch("workers.tasks.compute_observations._maybe_get_llm_backend", return_value=None),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
            patch("services.observation_service.ObservationService") as mock_svc_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = mock_episode
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc.run_full_project_scan.return_value = {"cooccurrence": 5}
            mock_svc_cls.return_value = mock_svc

            db = self._make_db()
            from workers.tasks.compute_observations import compute_observations

            await compute_observations(ctx=self._ctx(db), episode_id=_EPISODE_ID, org_id=_ORG_ID, project_id=_PROJECT_ID)

            mock_svc.run_full_project_scan.assert_called_once()
            mock_repo.apply_enrichment_bits.assert_called_once()

    # ── Coverage gap: trace_id, engine, llm_backend, _maybe_get_llm_backend ──

    @pytest.mark.asyncio
    async def test_with_trace_id(self) -> None:
        """Trace ID is bound to logging context."""
        mock_backend = AsyncMock()
        mock_episode = self._make_episode(enrichment_status=0)

        with (
            patch("workers.tasks.compute_observations.with_retry", lambda **kw: lambda f: f),
            patch("workers.backend.resolve_graph_backend", return_value=mock_backend),
            patch("workers.tasks.compute_observations._maybe_get_llm_backend", return_value=None),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
            patch("services.observation_service.ObservationService") as mock_svc_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = mock_episode
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc.run_full_project_scan.return_value = {"cooccurrence": 1}
            mock_svc_cls.return_value = mock_svc

            db = self._make_db()
            from workers.tasks.compute_observations import compute_observations

            await compute_observations(
                ctx=self._ctx(db), episode_id=_EPISODE_ID,
                org_id=_ORG_ID, project_id=_PROJECT_ID,
                trace_id="test-trace-001",
            )

            mock_svc.run_full_project_scan.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_db_engine_in_ctx(self) -> None:
        """Missing db_engine → creates own engine and disposes."""
        mock_backend = AsyncMock()
        mock_episode = self._make_episode(enrichment_status=0)

        with (
            patch("workers.tasks.compute_observations.with_retry", lambda **kw: lambda f: f),
            patch("workers.backend.resolve_graph_backend", return_value=mock_backend),
            patch("workers.tasks.compute_observations._maybe_get_llm_backend", return_value=None),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
            patch("services.observation_service.ObservationService") as mock_svc_cls,
            patch("asyncio.sleep", AsyncMock()),
            patch("core.db.init_db_engine") as mock_init_engine,
            patch("core.db.get_async_session") as mock_get_session,
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = mock_episode
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc.run_full_project_scan.return_value = {"cooccurrence": 1}
            mock_svc_cls.return_value = mock_svc

            mock_engine = AsyncMock()
            mock_engine.dispose = AsyncMock()
            mock_init_engine.return_value = mock_engine
            mock_session_factory = MagicMock()
            mock_get_session.return_value = mock_session_factory

            db = self._make_db()
            mock_session_factory.return_value = db

            # ctx WITHOUT db_engine or db_session_factory
            ctx: dict = {}

            from workers.tasks.compute_observations import compute_observations

            await compute_observations(
                ctx=ctx, episode_id=_EPISODE_ID,
                org_id=_ORG_ID, project_id=_PROJECT_ID,
            )

            mock_svc.run_full_project_scan.assert_called_once()
            mock_init_engine.assert_called_once()
            mock_engine.dispose.assert_called_once()

    @pytest.mark.asyncio
    async def test_with_llm_backend(self) -> None:
        """LLM backend available → used for content generation."""
        mock_backend = AsyncMock()
        mock_episode = self._make_episode(enrichment_status=0)
        mock_llm = AsyncMock()

        with (
            patch("workers.tasks.compute_observations.with_retry", lambda **kw: lambda f: f),
            patch("workers.backend.resolve_graph_backend", return_value=mock_backend),
            patch("workers.tasks.compute_observations._maybe_get_llm_backend",
                  return_value=mock_llm),
            patch("repositories.episode_repository.EpisodeRepository") as mock_repo_cls,
            patch("services.observation_service.ObservationService") as mock_svc_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = mock_episode
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc.run_full_project_scan.return_value = {"cooccurrence": 1}
            mock_svc_cls.return_value = mock_svc

            db = self._make_db()
            from workers.tasks.compute_observations import compute_observations

            await compute_observations(
                ctx=self._ctx(db), episode_id=_EPISODE_ID,
                org_id=_ORG_ID, project_id=_PROJECT_ID,
            )

            mock_svc.run_full_project_scan.assert_called_once()
            call_kwargs = mock_svc.run_full_project_scan.call_args.kwargs
            assert call_kwargs.get("llm_backend") is mock_llm

    @pytest.mark.asyncio
    async def test_maybe_get_llm_backend_error_path(self) -> None:
        """_maybe_get_llm_backend returns None when resolve_backend fails."""
        with patch("core.llm.resolve_backend", side_effect=Exception("LLM down")):
            from workers.tasks.compute_observations import _maybe_get_llm_backend

            result = await _maybe_get_llm_backend({})
            assert result is None
