"""Unit tests for classify_dialog task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_CONTENT = "Hello, how can I help you today?"
_TRACE_ID = "trace-003"


@pytest.mark.unit
class TestClassifyDialog:
    """classify_dialog task tests."""

    def _make_db(self) -> AsyncMock:
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        # Prevent .scalars().all() chains from returning coroutines
        db.execute.return_value = MagicMock()
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

    def _make_llm_response(
        self,
        intent: str = "greeting",
        emotion: str = "positive",
        valence: str = "positive",
        arousal: str = "medium",
        confidence: float = 0.95,
    ) -> MagicMock:
        """Create a mock LLM response with classification fields.

        The task accesses ``parsed.intent``, ``parsed.emotion``,
        ``parsed.valence``, ``parsed.arousal``, ``parsed.confidence``,
        and ``parsed.model_dump()`` directly, so we set them as mock
        attributes.
        """
        parsed = MagicMock()
        parsed.intent = intent
        parsed.emotion = emotion
        parsed.valence = valence
        parsed.arousal = arousal
        parsed.confidence = confidence
        parsed.model_dump.return_value = {
            "intent": intent,
            "emotion": emotion,
            "valence": valence,
            "arousal": arousal,
            "confidence": confidence,
        }
        resp = MagicMock()
        resp.validated_data = parsed
        return resp

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Dialog classified and persisted successfully."""
        llm_resp = self._make_llm_response()

        with (
            patch("workers.tasks.classify_dialog.render_prompt",
                  return_value=("system", {"schemas": []})),
            patch("workers.tasks.classify_dialog.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
        ):
            mock_llm = AsyncMock()
            mock_llm.chat.return_value = llm_resp
            mock_llm_cls.return_value = mock_llm

            mock_org_cfg.return_value = MagicMock()

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0
            episode.categories = None

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            from workers.tasks.classify_dialog import classify_dialog

            await classify_dialog(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
                trace_id=_TRACE_ID,
            )

            mock_llm.chat.assert_called_once()
            mock_ep_repo.apply_enrichment_bits.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_classified(self) -> None:
        """Episode already classified → skip."""
        with (
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
        ):
            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 1 << 4  # ENRICHMENT_CLASSIFICATION bit
            episode.categories = ["existing"]

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            from workers.tasks.classify_dialog import classify_dialog

            await classify_dialog(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
            )

            mock_ep_repo.apply_enrichment_bits.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_category(self) -> None:
        """Categories outside known taxonomy are stored as-is (validated by caller)."""
        llm_resp = self._make_llm_response(intent="unknown_category_xyz")

        with (
            patch("workers.tasks.classify_dialog.render_prompt",
                  return_value=("system", {"schemas": []})),
            patch("workers.tasks.classify_dialog.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
        ):
            mock_llm = AsyncMock()
            mock_llm.chat.return_value = llm_resp
            mock_llm_cls.return_value = mock_llm

            mock_org_cfg.return_value = MagicMock()

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0
            episode.categories = None

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            from workers.tasks.classify_dialog import classify_dialog

            await classify_dialog(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                content=_CONTENT,
            )

            mock_llm.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_episode_not_found(self) -> None:
        """Missing episode raises exception."""
        with (
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
        ):
            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = None
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            from workers.tasks.classify_dialog import classify_dialog

            with pytest.raises(Exception):
                await classify_dialog(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_llm_error(self) -> None:
        """LLM call failure propagates."""
        with (
            patch("workers.tasks.classify_dialog.render_prompt",
                  return_value=("system", {"schemas": []})),
            patch("workers.tasks.classify_dialog.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
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
            from workers.tasks.classify_dialog import classify_dialog

            with pytest.raises(Exception, match="LLM error"):
                await classify_dialog(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )
