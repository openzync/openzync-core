"""Unit tests for the dialog-classification post-processing helper.

The standalone ``classify_dialog`` ARQ task was retired in favour of the
combined ``enrich_episode`` worker (which calls ``process_classification_output``
as its classification section).  These tests exercise the helper directly —
label validation, value clamping, and the enrichment bit.  The caller-owned
orchestration (LLM call, idempotency, episode not found) is covered by
``test_enrich_episode.py``; the INSERT SQL structure is covered by
``test_process_classification_output.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from schemas.llm_outputs import ClassificationOutput
from workers.tasks.classify_dialog import process_classification_output

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())


@pytest.mark.unit
class TestProcessClassificationOutput:
    """process_classification_output label validation and persistence."""

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        """Return a mock async DB session with ``execute`` + ``flush``."""
        m = AsyncMock()
        m.execute = AsyncMock()
        m.flush = AsyncMock()
        return m

    @pytest.fixture
    def validation_sets(self) -> dict[str, set[str]]:
        """Label sets that accept the values in *valid_output*."""
        return {
            "intent_set": {"greeting"},
            "emotion_set": {"positive"},
        }

    def _valid_output(self) -> ClassificationOutput:
        return ClassificationOutput(
            intent="greeting",
            emotion="positive",
            valence="positive",
            arousal="medium",
            confidence=0.95,
        )

    @pytest.mark.asyncio
    async def test_valid_labels_persisted(
        self,
        mock_db: AsyncMock,
        validation_sets: dict[str, set[str]],
    ) -> None:
        """Known intent/emotion/valence/arousal are persisted as-is."""
        await process_classification_output(
            db=mock_db,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=self._valid_output(),
            validation_sets=validation_sets,
        )

        params = mock_db.execute.await_args.args[1]
        assert params["intent"] == "greeting"
        assert params["emotion"] == "positive"
        assert params["valence"] == "positive"
        assert params["arousal"] == "medium"
        assert params["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_unknown_category_is_dropped(
        self,
        mock_db: AsyncMock,
        validation_sets: dict[str, set[str]],
    ) -> None:
        """Intent outside the org's taxonomy is not persisted (validated)."""
        parsed = self._valid_output().model_copy(update={"intent": "unknown_category_xyz"})

        await process_classification_output(
            db=mock_db,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=parsed,
            validation_sets=validation_sets,
        )

        params = mock_db.execute.await_args.args[1]
        assert params["intent"] is None

    @pytest.mark.asyncio
    async def test_unknown_emotion_is_dropped(
        self,
        mock_db: AsyncMock,
        validation_sets: dict[str, set[str]],
    ) -> None:
        """Emotion outside the org's taxonomy is not persisted."""
        parsed = self._valid_output().model_copy(update={"emotion": "not-a-real-emotion"})

        await process_classification_output(
            db=mock_db,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=parsed,
            validation_sets=validation_sets,
        )

        params = mock_db.execute.await_args.args[1]
        assert params["emotion"] is None

    @pytest.mark.asyncio
    async def test_invalid_valence_and_arousal_dropped(
        self,
        mock_db: AsyncMock,
        validation_sets: dict[str, set[str]],
    ) -> None:
        """Valence/arousal outside the fixed taxonomy are not persisted."""
        parsed = self._valid_output().model_copy(
            update={"valence": "euphoric", "arousal": "extreme"}
        )

        await process_classification_output(
            db=mock_db,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=parsed,
            validation_sets=validation_sets,
        )

        params = mock_db.execute.await_args.args[1]
        assert params["valence"] is None
        assert params["arousal"] is None

    @pytest.mark.asyncio
    async def test_confidence_clamped_to_unit_range(
        self,
        mock_db: AsyncMock,
        validation_sets: dict[str, set[str]],
    ) -> None:
        """Out-of-range confidence is clamped to [0.0, 1.0]."""
        parsed = self._valid_output().model_copy(update={"confidence": 1.7})

        await process_classification_output(
            db=mock_db,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=parsed,
            validation_sets=validation_sets,
        )

        params = mock_db.execute.await_args.args[1]
        assert params["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_enrichment_bit_set_when_repo_provided(
        self,
        mock_db: AsyncMock,
        validation_sets: dict[str, set[str]],
    ) -> None:
        """The ENRICHMENT_CLASSIFICATION bit is applied + flushed."""
        from workers.tasks.base import ENRICHMENT_CLASSIFICATION

        episode_repo = AsyncMock()

        await process_classification_output(
            db=mock_db,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=self._valid_output(),
            validation_sets=validation_sets,
            episode_repo=episode_repo,
        )

        episode_repo.apply_enrichment_bits.assert_awaited_once_with(
            UUID(_EPISODE_ID), ENRICHMENT_CLASSIFICATION
        )
        mock_db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_repo_skips_enrichment_bit(
        self,
        mock_db: AsyncMock,
        validation_sets: dict[str, set[str]],
    ) -> None:
        """Without an episode_repo, no enrichment bit is set."""
        await process_classification_output(
            db=mock_db,
            org_id=_ORG_ID,
            episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID,
            parsed=self._valid_output(),
            validation_sets=validation_sets,
        )

        mock_db.flush.assert_awaited_once()
