"""Unit tests for extract_facts task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_CONTENT = "Alice works at Acme Corp in San Francisco."
_TRACE_ID = "trace-002"


@pytest.mark.unit
class TestExtractFacts:
    """extract_facts task tests."""

    def _make_db(self) -> AsyncMock:
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        # Prevent .scalars().all() chains from returning coroutines
        result = MagicMock()
        result.scalar_one_or_none.return_value = str(uuid4())
        db.execute.return_value = result
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

    @staticmethod
    def _make_llm_response(facts: list[dict] | None = None) -> MagicMock:
        """Create a mock LLM response with parsed facts.

        The task iterates ``parsed.facts`` and calls ``fact.model_dump()`` on
        each element, so we build a list of MagicMock objects with
        model_dump set up.
        """
        fact_dicts = facts or [
            {
                "subject": "Alice",
                "predicate": "works_at",
                "object": "Acme Corp",
                "confidence": 0.95,
            },
        ]
        fact_mocks = []
        for fd in fact_dicts:
            fm = MagicMock()
            fm.model_dump.return_value = fd
            fact_mocks.append(fm)

        parsed = MagicMock()
        parsed.facts = fact_mocks
        resp = MagicMock()
        resp.validated_data = parsed
        return resp

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Facts extracted and persisted successfully."""
        llm_resp = self._make_llm_response()

        with (
            patch("workers.tasks.extract_facts.render_prompt",
                  return_value=("system", {})),
            patch("workers.tasks.extract_facts.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.backend.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("repositories.fact_repository.FactRepository") as mock_fact_repo_cls,
            patch("core.config.settings") as _,
        ):
            mock_llm = AsyncMock()
            mock_llm.chat.return_value = llm_resp
            mock_llm_cls.return_value = mock_llm

            mock_org_cfg.return_value = MagicMock()

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            mock_fact = MagicMock()
            mock_fact.id = str(uuid4())
            mock_fact.content = "Alice works_at Acme Corp"
            mock_fact_repo = AsyncMock()
            mock_fact_repo.batch_create_or_skip.return_value = [mock_fact]
            mock_fact_repo_cls.return_value = mock_fact_repo

            db = self._make_db()
            from workers.tasks.extract_facts import extract_facts

            await extract_facts(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
                trace_id=_TRACE_ID,
            )

            mock_llm.chat.assert_called_once()
            mock_fact_repo.batch_create_or_skip.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_facts(self) -> None:
        """No facts extracted → no bulk_create."""
        parsed = MagicMock()
        parsed.facts = []
        llm_resp = MagicMock()
        llm_resp.validated_data = parsed

        with (
            patch("workers.tasks.extract_facts.render_prompt",
                  return_value=("system", {})),
            patch("workers.tasks.extract_facts.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.backend.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("repositories.fact_repository.FactRepository") as mock_fact_repo_cls,
            patch("core.config.settings") as _,
        ):
            mock_llm = AsyncMock()
            mock_llm.chat.return_value = llm_resp
            mock_llm_cls.return_value = mock_llm

            mock_org_cfg.return_value = MagicMock()

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            mock_fact_repo = AsyncMock()
            mock_fact_repo_cls.return_value = mock_fact_repo

            db = self._make_db()
            from workers.tasks.extract_facts import extract_facts

            await extract_facts(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
            )

            mock_llm.chat.assert_called_once()
            mock_fact_repo.batch_create_or_skip.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_confidence_filtered(self) -> None:
        """Low-confidence facts are filtered out."""
        llm_resp = self._make_llm_response([
            {"subject": "Alice", "predicate": "works_at", "object": "Acme Corp", "confidence": 0.95},
            {"subject": "Bob", "predicate": "might_work_at", "object": "Unknown", "confidence": 0.3},
        ])

        with (
            patch("workers.tasks.extract_facts.render_prompt",
                  return_value=("system", {})),
            patch("workers.tasks.extract_facts.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.backend.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("repositories.fact_repository.FactRepository") as mock_fact_repo_cls,
            patch("core.config.settings") as _,
        ):
            mock_llm = AsyncMock()
            mock_llm.chat.return_value = llm_resp
            mock_llm_cls.return_value = mock_llm

            mock_org_cfg.return_value = MagicMock()

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            mock_fact_repo = AsyncMock()
            mock_fact_repo_cls.return_value = mock_fact_repo

            db = self._make_db()
            from workers.tasks.extract_facts import extract_facts

            await extract_facts(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
            )

            mock_fact_repo.batch_create_or_skip.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_error(self) -> None:
        """LLM call failure propagates."""
        with (
            patch("workers.tasks.extract_facts.render_prompt",
                  return_value=("system", {})),
            patch("workers.tasks.extract_facts.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.backend.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.config.settings") as _,
        ):
            mock_llm = AsyncMock()
            mock_llm.chat.side_effect = Exception("LLM error")
            mock_llm_cls.return_value = mock_llm

            mock_org_cfg.return_value = MagicMock()

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            from workers.tasks.extract_facts import extract_facts

            with pytest.raises(Exception, match="LLM error"):
                await extract_facts(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_episode_not_found(self) -> None:
        """Missing episode raises exception."""
        with (
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.config.settings") as _,
        ):
            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = None
            mock_ep_repo_cls.return_value = mock_ep_repo

            mock_org_cfg.return_value = MagicMock()

            db = self._make_db()
            from workers.tasks.extract_facts import extract_facts

            with pytest.raises(Exception):
                await extract_facts(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_incomplete_triple_filtered(self) -> None:
        """Facts missing subject/predicate/object are filtered out."""
        llm_resp = self._make_llm_response([
            {"subject": "Alice", "predicate": "works_at", "object": "Acme Corp", "confidence": 0.95},
            {"subject": "", "predicate": "is", "object": "Unknown", "confidence": 0.8},
        ])

        with (
            patch("workers.tasks.extract_facts.render_prompt",
                  return_value=("system", {})),
            patch("workers.tasks.extract_facts.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.backend.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("repositories.fact_repository.FactRepository") as mock_fact_repo_cls,
            patch("core.config.settings") as _,
        ):
            mock_llm = AsyncMock()
            mock_llm.chat.return_value = llm_resp
            mock_llm_cls.return_value = mock_llm

            mock_org_cfg.return_value = MagicMock()

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            mock_fact = MagicMock()
            mock_fact.id = str(uuid4())
            mock_fact.content = "Alice works_at Acme Corp"
            mock_fact_repo = AsyncMock()
            mock_fact_repo.batch_create_or_skip.return_value = [mock_fact]
            mock_fact_repo_cls.return_value = mock_fact_repo

            db = self._make_db()
            from workers.tasks.extract_facts import extract_facts

            await extract_facts(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
            )

            mock_fact_repo.batch_create_or_skip.assert_called_once()
