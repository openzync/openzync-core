"""Unit tests for CustomInstructionRepository."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.custom_instruction_repository import CustomInstructionRepository


pytestmark = pytest.mark.unit


class TestCustomInstructionRepository:
    """CustomInstructionRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    TARGET_ID = UUID("00000000-0000-0000-0000-000000000010")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> CustomInstructionRepository:
        return CustomInstructionRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_instruction(self, **overrides: object) -> MagicMock:
        inst = MagicMock()
        inst.id = overrides.get("id", uuid4())
        inst.organization_id = overrides.get("organization_id", self.ORG_ID)
        inst.scope = overrides.get("scope", "extraction")
        inst.target_id = overrides.get("target_id", None)
        inst.name = overrides.get("name", "test-instruction")
        inst.text = overrides.get("text", "Do the thing")
        return inst

    # ── get_by_scope ───────────────────────────────────────────────────────────

    async def test_get_by_scope_org_level(
        self, repo: CustomInstructionRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_scope returns org-level instructions when target_id is None."""
        instructions = [
            self._mock_instruction(scope="extraction"),
            self._mock_instruction(scope="extraction"),
        ]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = instructions
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_scope(
            org_id=self.ORG_ID, scope="extraction"
        )

        assert result == instructions

    async def test_get_by_scope_target_level(
        self, repo: CustomInstructionRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_scope returns target-specific instructions when target_id given."""
        instructions = [self._mock_instruction(target_id=self.TARGET_ID)]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = instructions
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_scope(
            org_id=self.ORG_ID, scope="user_summary", target_id=self.TARGET_ID
        )

        assert result == instructions

    async def test_get_by_scope_empty(
        self, repo: CustomInstructionRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_scope returns empty list when no instructions match."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_scope(
            org_id=self.ORG_ID, scope="nonexistent"
        )

        assert result == []

    # ── set_by_scope ───────────────────────────────────────────────────────────

    async def test_set_by_scope_replaces_instructions(
        self, repo: CustomInstructionRepository, mock_db: AsyncMock
    ) -> None:
        """set_by_scope deletes old and bulk-inserts new instructions."""
        instructions = [
            {"name": "instr1", "text": "Do one thing"},
            {"name": "instr2", "text": "Do another thing"},
        ]
        mock_db.execute.return_value = MagicMock()
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.set_by_scope(
            org_id=self.ORG_ID,
            scope="extraction",
            target_id=None,
            instructions=instructions,
        )

        assert len(result) == 2
        assert mock_db.add.call_count == 2
        mock_db.flush.assert_awaited_once()
        # refresh called for each new instruction
        assert mock_db.refresh.await_count == 2

    async def test_set_by_scope_empty_instructions(
        self, repo: CustomInstructionRepository, mock_db: AsyncMock
    ) -> None:
        """set_by_scope with empty list deletes existing and returns empty."""
        mock_db.execute.return_value = MagicMock()

        result = await repo.set_by_scope(
            org_id=self.ORG_ID,
            scope="extraction",
            target_id=None,
            instructions=[],
        )

        assert result == []
        mock_db.flush.assert_awaited_once()

    async def test_set_by_scope_with_target(
        self, repo: CustomInstructionRepository, mock_db: AsyncMock
    ) -> None:
        """set_by_scope scopes delete and insert by target_id."""
        instructions = [{"name": "instr", "text": "text"}]
        mock_db.execute.return_value = MagicMock()
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.set_by_scope(
            org_id=self.ORG_ID,
            scope="user_summary",
            target_id=self.TARGET_ID,
            instructions=instructions,
        )

        assert len(result) == 1

    # ── delete_by_scope ────────────────────────────────────────────────────────

    async def test_delete_by_scope(
        self, repo: CustomInstructionRepository, mock_db: AsyncMock
    ) -> None:
        """delete_by_scope removes instructions for the given scope."""
        mock_db.execute.return_value = MagicMock()

        await repo.delete_by_scope(
            org_id=self.ORG_ID, scope="extraction"
        )

        mock_db.execute.assert_awaited_once()
        mock_db.flush.assert_awaited_once()

    async def test_delete_by_scope_with_target(
        self, repo: CustomInstructionRepository, mock_db: AsyncMock
    ) -> None:
        """delete_by_scope removes target-scoped instructions."""
        mock_db.execute.return_value = MagicMock()

        await repo.delete_by_scope(
            org_id=self.ORG_ID,
            scope="user_summary",
            target_id=self.TARGET_ID,
        )

        mock_db.execute.assert_awaited_once()
        mock_db.flush.assert_awaited_once()

    async def test_delete_by_scope_noop_on_empty(
        self, repo: CustomInstructionRepository, mock_db: AsyncMock
    ) -> None:
        """delete_by_scope runs the delete query even if nothing matches."""
        mock_db.execute.return_value = MagicMock()
        mock_db.flush.return_value = None

        await repo.delete_by_scope(
            org_id=self.ORG_ID, scope="nonexistent"
        )

        mock_db.execute.assert_awaited_once()
        mock_db.flush.assert_awaited_once()
