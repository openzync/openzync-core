"""Unit tests for process_classification_output — ON CONFLICT DO NOTHING.

``process_classification_output`` inserts a classification row with an
``ON CONFLICT (organization_id, episode_id) DO NOTHING`` clause, making the
operation idempotent: calling it twice with the same ``episode_id`` does not
raise a unique-constraint violation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from schemas.llm_outputs import ClassificationOutput
from workers.tasks.classify_dialog import process_classification_output


@pytest.mark.unit
class TestProcessClassificationOutput:
    """process_classification_output inserts with ON CONFLICT DO NOTHING."""

    ORG_ID = str(uuid4())
    EPISODE_ID = str(uuid4())
    PROJECT_ID = str(uuid4())

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        """Return a mock async DB session with execute + flush.

        ``AsyncMock`` child attributes are regular ``MagicMock`` by default,
        which breaks ``await db.execute(...)``.  Explicitly assigning
        ``AsyncMock`` instances to each coroutine-method fixes this.
        """
        m = AsyncMock()
        m.execute = AsyncMock()
        m.flush = AsyncMock()
        return m

    @pytest.fixture
    def valid_output(self) -> ClassificationOutput:
        """Return a ClassificationOutput with valid label values."""
        return ClassificationOutput(
            intent="greeting",
            emotion="positive",
            valence="positive",
            arousal="medium",
            confidence=0.95,
        )

    @pytest.fixture
    def validation_sets(self) -> dict[str, set[str]]:
        """Return validation sets that accept the values in *valid_output*."""
        return {
            "intent_set": {"greeting"},
            "emotion_set": {"positive"},
        }

    # ── SQL structure assertions ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_sql_contains_on_conflict(
        self,
        mock_db: AsyncMock,
        valid_output: ClassificationOutput,
        validation_sets: dict[str, set[str]],
    ) -> None:
        """The INSERT statement includes ON CONFLICT … DO NOTHING."""
        await process_classification_output(
            db=mock_db,
            org_id=self.ORG_ID,
            episode_id=self.EPISODE_ID,
            project_id=self.PROJECT_ID,
            parsed=valid_output,
            validation_sets=validation_sets,
        )

        assert mock_db.execute.await_count == 1
        call = mock_db.execute.await_args_list[0]
        sql_text = str(call.args[0])
        assert "ON CONFLICT" in sql_text
        assert "DO NOTHING" in sql_text

    # ── Idempotency assertions ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_idempotent_on_duplicate_call(
        self,
        mock_db: AsyncMock,
        valid_output: ClassificationOutput,
        validation_sets: dict[str, set[str]],
    ) -> None:
        """Calling twice with the same episode_id does not raise."""
        # First call
        await process_classification_output(
            db=mock_db,
            org_id=self.ORG_ID,
            episode_id=self.EPISODE_ID,
            project_id=self.PROJECT_ID,
            parsed=valid_output,
            validation_sets=validation_sets,
        )

        # Second call — ON CONFLICT DO NOTHING makes this safe
        await process_classification_output(
            db=mock_db,
            org_id=self.ORG_ID,
            episode_id=self.EPISODE_ID,
            project_id=self.PROJECT_ID,
            parsed=valid_output,
            validation_sets=validation_sets,
        )

        assert mock_db.execute.await_count == 2

    # ── Edge cases ──────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_db_flush_called(
        self,
        mock_db: AsyncMock,
        valid_output: ClassificationOutput,
        validation_sets: dict[str, set[str]],
    ) -> None:
        """A flush is triggered after the insert."""
        await process_classification_output(
            db=mock_db,
            org_id=self.ORG_ID,
            episode_id=self.EPISODE_ID,
            project_id=self.PROJECT_ID,
            parsed=valid_output,
            validation_sets=validation_sets,
        )

        mock_db.flush.assert_awaited_once()
