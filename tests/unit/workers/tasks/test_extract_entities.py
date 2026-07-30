"""Unit tests for extract_entities task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.exceptions import GraphBackendUnavailableError

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_ENT_1_ID = str(uuid4())
_CONTENT = "Alice and Bob discussed the quarterly report."
_TRACE_ID = "trace-001"


@pytest.mark.unit
class TestExtractEntities:
    """extract_entities task tests."""

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

    def _make_entity(self, name: str, type: str = "person",
                     mentions: list[str] | None = None) -> MagicMock:
        """Create a mock EntityOutput with a ``.model_dump()`` that returns a dict."""
        e = MagicMock()
        e.model_dump.return_value = {
            "name": name,
            "type": type,
            "summary": f"{name} ({type})",
            "mentions": mentions or [name],
        }
        return e

    def _make_llm_response(self, entities: list[MagicMock] | None = None) -> MagicMock:
        """Create a mock LLM response with accessible entities/relationships.

        The task iterates ``parsed.entities`` and calls ``.model_dump()`` on
        each item to convert EntityOutput → dict.  We set up ``model_dump``
        on each mock entity so the dict contains usable name/type/mentions.
        """
        if entities is None:
            entities = [self._make_entity("Alice"), self._make_entity("Bob")]

        parsed = MagicMock()
        parsed.entities = entities
        parsed.relationships = []
        resp = MagicMock()
        resp.validated_data = parsed
        return resp

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Entities extracted and persisted successfully."""
        llm_resp = self._make_llm_response()

        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.extract_entities.render_prompt",
                  return_value=("system", {})),
            patch("workers.tasks.extract_entities.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.tasks.extract_entities.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("repositories.entity_repository.EntityRepository") as mock_ent_repo_cls,
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

            mock_ent_repo = AsyncMock()
            mock_ent_repo.upsert_entity.return_value = {"id": _ENT_1_ID}
            mock_ent_repo_cls.return_value = mock_ent_repo

            db = self._make_db()
            from workers.tasks.extract_entities import extract_entities

            await extract_entities(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
                trace_id=_TRACE_ID,
            )

            mock_llm.chat.assert_called_once()
            assert mock_ent_repo.upsert_entity.call_count >= 1
            mock_ep_repo.apply_enrichment_bits.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_entities(self) -> None:
        """No entities extracted → still handles gracefully."""
        parsed = MagicMock()
        parsed.entities = []
        parsed.relationships = []
        llm_resp = MagicMock()
        llm_resp.validated_data = parsed

        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.extract_entities.render_prompt",
                  return_value=("system", {})),
            patch("workers.tasks.extract_entities.build_enrichment_prompt",
                  return_value="prompt"),
            patch("workers.tasks.extract_entities.render_prompt",
                  return_value=("system", {})),
            patch("workers.tasks.extract_entities.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.tasks.extract_entities.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("repositories.entity_repository.EntityRepository") as mock_ent_repo_cls,
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

            mock_ent_repo = AsyncMock()
            mock_ent_repo_cls.return_value = mock_ent_repo

            db = self._make_db()
            from workers.tasks.extract_entities import extract_entities

            await extract_entities(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
            )

            mock_llm.chat.assert_called_once()
            mock_ent_repo.upsert_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_pronouns_filtered(self) -> None:
        """Pronoun-like entities are filtered out."""
        llm_resp = self._make_llm_response([
            self._make_entity("Alice"),
            self._make_entity("I", type="pronoun"),
        ])

        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.extract_entities.render_prompt",
                  return_value=("system", {})),
            patch("workers.tasks.extract_entities.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.tasks.extract_entities.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("repositories.entity_repository.EntityRepository") as mock_ent_repo_cls,
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

            mock_ent_repo = AsyncMock()
            mock_ent_repo.upsert_entity.return_value = {"id": _ENT_1_ID}
            mock_ent_repo_cls.return_value = mock_ent_repo

            db = self._make_db()
            from workers.tasks.extract_entities import extract_entities

            await extract_entities(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
            )

            mock_ent_repo.upsert_entity.assert_called_once()
            assert mock_ent_repo.upsert_entity.call_args[1]["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_llm_error(self) -> None:
        """LLM call failure propagates."""
        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.extract_entities.render_prompt",
                  return_value=("system", {})),
            patch("workers.tasks.extract_entities.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.tasks.extract_entities.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
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
            from workers.tasks.extract_entities import extract_entities

            with pytest.raises(Exception, match="LLM error"):
                await extract_entities(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_graph_backend_unavailable(self) -> None:
        """Graph backend returns None → handled gracefully."""
        llm_resp = self._make_llm_response()

        with (
            patch("workers.tasks.base.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.extract_entities.render_prompt",
                  return_value=("system", {})),
            patch("workers.tasks.extract_entities.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.tasks.extract_entities.resolve_graph_backend", return_value=None),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("repositories.entity_repository.EntityRepository") as mock_ent_repo_cls,
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

            mock_ent_repo = AsyncMock()
            mock_ent_repo_cls.return_value = mock_ent_repo

            db = self._make_db()
            from workers.tasks.extract_entities import extract_entities

            with pytest.raises(GraphBackendUnavailableError):
                await extract_entities(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )

            mock_llm.chat.assert_called()
